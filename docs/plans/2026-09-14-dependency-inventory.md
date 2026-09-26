# Fathom v0.3.0 依赖与资源来源清单（SBOM 等价物）

- 日期：2026-09-14（ISS-037）。本清单是发行（ISS-041）与许可证选择
  （[2026-09-14-license-options.md](2026-09-14-license-options.md)）的输入。
- 基线：分支 `iss-037-version-source`，Cargo.lock / requirements / vendor
  引用点取自 main `f035d92`（PR #48 合并后）。
- 方法：本地文件事实（Cargo.lock、requirements-*.txt、vendor 文件头）＋
  crates.io API / PyPI JSON / npm registry / docs.python.org 在线元数据核对
  （核对时间 2026-09-14，均为时点数据）。**凡未核对处标 UNKNOWN，不猜测。**
- 配套 notices 见 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。

## 1. Python 运行时依赖（随冻结 helper 进入分发产物）

直接 pin（[requirements-runtime.txt](../../packaging/requirements-runtime.txt)，安装需
PM 批准的提案状态不变）：

| 组件 | 版本 | 许可证 | 来源 URL | 证据 |
|---|---|---|---|---|
| fastapi | 0.141.1（pin） | MIT | https://pypi.org/project/fastapi/0.141.1/ | PyPI `license_expression: "MIT"`，license_files=[LICENSE] |
| uvicorn | 0.52.4（pin） | BSD-3-Clause | https://pypi.org/project/uvicorn/0.52.4/ | PyPI `license_expression: "BSD-3-Clause"` |

传递依赖（按 PyPI `requires_dist` 约束列出；**精确版本未锁定**——完整
pip freeze lock 快照按 requirements-runtime.txt 注释归 ISS-031/ISS-009 冻结
证据，届时回填本表）：

| 组件 | 约束 | 许可证 | 来源 URL | 证据 |
|---|---|---|---|---|
| starlette | >=0.46.0 | BSD-3-Clause | https://pypi.org/project/starlette/ | PyPI（1.6.0 元数据 `license_expression: "BSD-3-Clause"`） |
| pydantic | >=2.9.0 | MIT | https://github.com/pydantic/pydantic/blob/main/LICENSE | 上游仓库 LICENSE 文件（"The MIT License (MIT)"） |
| click | >=7.0 | BSD-3-Clause | https://pypi.org/project/click/ | PyPI（8.5.0 元数据） |
| h11 | >=0.8 | MIT | https://pypi.org/project/h11/ | PyPI license 字段 "MIT"（license_expression 为动态、空） |
| typing-extensions | >=4.8.0 | UNKNOWN | https://pypi.org/project/typing-extensions/ | 未核对 |
| typing-inspection | >=0.4.2 | UNKNOWN | https://pypi.org/project/typing-inspection/ | 未核对 |
| annotated-doc | >=0.0.2 | UNKNOWN | https://pypi.org/project/annotated-doc/ | 未核对 |

httpx / pytest 仅测试用，不进入发行运行时（requirements-runtime.txt 注释）。

## 2. 构建工具链

| 组件 | 版本 | 许可证 | 来源 URL | 证据与影响 |
|---|---|---|---|---|
| pyinstaller | 6.22.3（pin，需授权安装） | GPLv2-or-later **+ bootloader 例外** | https://pypi.org/project/pyinstaller/6.22.3/ | PyPI license 字段原文："GPLv2-or-later with a special exception which allows to use PyInstaller to build and distribute non-free programs (including commercial ones)"。例外明确允许用其构建并分发非自由（含商用）程序；对项目 LICENSE 选择的影响见 license-options 兼容矩阵 |
| pyinstaller 构建期传递（altgraph、macholib、packaging、pyinstaller-hooks-contrib、setuptools） | 随工具链 | UNKNOWN | — | 仅构建期使用、不进入分发产物；未逐一核对 |
| CPython（冻结产物内嵌解释器） | 3.14.6 | PSF-2.0（"PYTHON SOFTWARE FOUNDATION LICENSE VERSION 2"） | https://docs.python.org/3/license.html | OSI 认可开源；分发需保留 PSF 协议与版权声明、衍生版本需附改动摘要（见 THIRD_PARTY_NOTICES.md） |

macOS 系统框架（WebKit/WKWebView 经 wry/tao 调用）为系统组件，不随产物
分发，不计入本清单。

## 3. Rust / Tauri crate（apps/desktop/src-tauri/Cargo.lock）

直接依赖（Cargo.toml 声明，版本取自 Cargo.lock @ f035d92）：

| crate | 锁定版本 | 许可证 | 来源 |
|---|---|---|---|
| tauri | 2.11.5 | Apache-2.0 OR MIT | https://crates.io/crates/tauri/2.11.5 |
| tauri-build | 2.6.3 | Apache-2.0 OR MIT | https://crates.io/crates/tauri-build/2.6.3 |
| tauri-plugin-opener | 2.5.5 | Apache-2.0 OR MIT | https://crates.io/crates/tauri-plugin-opener/2.5.5 |
| serde | 1.0.229 | MIT OR Apache-2.0 | https://crates.io/crates/serde/1.0.229 |
| serde_json | 1.0.151 | MIT OR Apache-2.0 | https://crates.io/crates/serde_json/1.0.151 |

主要传递依赖（对产物体积/行为影响最大的核对子集）：

| crate | 锁定版本 | 许可证 | 来源 |
|---|---|---|---|
| wry（WebView 绑定） | 0.55.1 | Apache-2.0 OR MIT | https://crates.io/crates/wry/0.55.1 |
| tao（窗口/事件循环） | 0.35.3 | **Apache-2.0（仅单许可）** | https://crates.io/crates/tao/0.35.3 |
| tokio | 1.53.1 | MIT | https://crates.io/crates/tokio/1.53.1 |
| objc2（Objective-C 运行时绑定） | 0.6.4 | MIT | https://crates.io/crates/objc2/0.6.4 |
| log | 0.4.34 | MIT OR Apache-2.0 | https://crates.io/crates/log/0.4.34 |
| thiserror | 1.0.69 / 2.0.20（多版本共存） | MIT OR Apache-2.0（2.0.20 已核对；1.0.69 同 crate 未单独核对） | https://crates.io/crates/thiserror/2.0.20 |
| libc | 0.2.189 | MIT OR Apache-2.0 | https://crates.io/crates/libc/0.2.189 |
| getrandom | 0.2.17 / 0.3.4 / 0.4.3（多版本共存） | MIT OR Apache-2.0（0.4.3 已核对；其余两个版本未单独核对） | https://crates.io/crates/getrandom/0.4.3 |
| semver | 1.0.28 | MIT OR Apache-2.0 | https://crates.io/crates/semver/1.0.28 |
| http | 1.5.0 | MIT OR Apache-2.0 | https://crates.io/crates/http/1.5.0 |
| url | 2.5.8 | MIT OR Apache-2.0 | https://crates.io/crates/url/2.5.8 |
| ico | 0.5.0 | MIT | https://crates.io/crates/ico/0.5.0 |
| png | 0.17.16 / 0.18.1（多版本共存） | MIT OR Apache-2.0（0.17.16 已核对；0.18.1 未单独核对） | https://crates.io/crates/png/0.17.16 |
| base64 | 0.21.7 / 0.22.1 / 0.23.1（多版本共存） | UNKNOWN | 未核对 |

**未核对范围（fail-closed 披露）**：Cargo.lock 共 474 个 package 版本条目
（含上述多版本共存重复计数），上表之外的传递 crate 许可证一律
UNKNOWN、完整清单以 Cargo.lock 为权威。发行（ISS-041）前如需全量核对，
可评估 cargo-deny/cargo-license（引入任何新工具依赖须 PM 授权——本任务
不安装任何依赖）。

## 4. 前端与图形资源

| 资源 | 版本 | 许可证 | 来源与状态 |
|---|---|---|---|
| frontend/vendor/echarts.min.js | 文件内 echarts 核心 export 标识 5.6.0 | Apache-2.0 | 文件头含 ASF Apache-2.0 完整声明（本地逐字在档）；npm registry echarts@5.6.0 元数据 license="Apache-2.0"、依赖 tslib 2.3.0 + zrender 5.6.1。**上游来源 URL 与 SHA-256 未记录：NOT_VERIFIED**——git 历史无下载来源；且文件内另有一处 `t.version="5.5.1"` 标识（归属内嵌模块，与 npm 5.6.0 声明的 zrender 5.6.1 不符），是否与任一官方 dist 逐字节一致未核对。**后续动作**：正式发行前从官方渠道（https://www.npmjs.com/package/echarts / jsDelivr）重新获取，回填本表 URL+SHA-256 并清理疑点 |
| echarts 内嵌 tslib（Microsoft 版权段，见文件头第二段声明） | 随 bundle（npm 元数据 2.3.0） | 0BSD | npm registry tslib@2.3.0 license="0BSD"；文件头版权段逐字在档 |
| echarts 内嵌 zrender | 随 bundle（npm 元数据 5.6.1） | 待补 | 随 echarts 官方 bundle 组合分发；未见独立版权头，按上游项目处理，单独核对来源：https://github.com/ecomfe/zrender |
| frontend/icons.js（SVG 图标集） | — | 自有（项目原创） | 无第三方图标库；AGENTS.md 规定 SVG 图标只在 icons.js 集中维护 |
| apps/desktop/src-tauri/icons/tray.png | — | 自有（项目原创） | scripts/make_tray_icon.py 纯 stdlib 生成 |
| apps/desktop/src-tauri/icons/icon.png | — | UNKNOWN（待 ISS-045 替换） | 来源未记录（疑似 Tauri 脚手架占位）；**正式图标为 ISS-045 人工门**，发行资产不得使用现状占位图标 |
| apps/desktop/frontend-dist/ | — | 构建产物副本 | 当前仅 index.html，来源同 frontend/ |

## 5. 版本一致性（发行 tag 门禁输入）

- 单一版本源：`fathom/__init__.py` `__version__ = "0.3.0"`；FastAPI
  `version=` 从该源读取；`tauri.conf.json` 与 `Cargo.toml` [package]
  version 与之同步。
- fail-closed 校验：`scripts/check_version_consistency.sh`（退出码合同
  0/1/2，CI 可直接调用），行为由 `tests/test_version_consistency.py`
  12 项测试钉住。
- 发行 tag 必须等于单一版本源（ISS-041 验收引用本节）。

## 6. 缺口与后续

1. 图标/Logo 正式资产：ISS-045 人工门（用户选择），当前 icon.png 来源
   UNKNOWN、不得作为发行资产。
2. echarts 上游核对：URL + SHA-256 回填（见 §4）；建议随 ISS-041 前置检查。
3. Python 传递依赖精确版本 lock（pip freeze 快照）：ISS-031/ISS-009 冻结
   证据回填本表 §1。
4. Cargo.lock 全量 474 条目的逐一许可核对：如需，评估 cargo-deny（新工具
   依赖须 PM 授权）。
5. LICENSE 落地：待用户在 license-options 方案中选定后新建（本任务明确
   不落 LICENSE）。
