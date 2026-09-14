# Third-Party Notices

Fathom（本仓库/分发产物）包含以下第三方软件与资源。本文件是
[依赖与来源清单](docs/plans/2026-09-14-dependency-inventory.md)（SBOM
等价物）的 notices 配套；版本、证据方式与未核对范围（UNKNOWN）以该清单
为准。**版权文本只摘录已核实的原文；未摘录处标"待补充"并给出来源 URL，
不伪造。**

许可证全文的规范文本（canonical）：

- MIT：https://opensource.org/license/MIT
- Apache-2.0：https://www.apache.org/licenses/LICENSE-2.0.txt
- BSD-3-Clause：https://opensource.org/license/BSD-3-Clause
- 0BSD：https://opensource.org/license/0BSD
- PSF-2.0：https://docs.python.org/3/license.html
- GPL-2.0 与 PyInstaller 例外：https://pyinstaller.org/en/stable/license.html

> 发行打包说明（ISS-041 输入）：按 Apache-2.0 §4.1 与各许可证要求，
> 对外分发的 .app/.dmg 需随产物附带（bundle 内或旁边）所列许可证全文
> 与本 notices；Apache-2.0 组件若上游含 NOTICE 文件须一并保留。

## 1. Python 运行时（随冻结 helper 分发）

| 组件 | 版本 | 版权（原文或待补充） | 许可证 | 来源 |
|---|---|---|---|---|
| FastAPI | 0.141.1 | 待补充（https://github.com/fastapi/fastapi/blob/master/LICENSE.txt） | MIT | https://pypi.org/project/fastapi/0.141.1/ |
| Uvicorn | 0.52.4 | 待补充（https://github.com/encode/uvicorn/blob/master/LICENSE.md） | BSD-3-Clause | https://pypi.org/project/uvicorn/0.52.4/ |
| Starlette | ≥0.46.0（未锁版本） | 待补充（https://github.com/encode/starlette/blob/master/LICENSE.md） | BSD-3-Clause | https://pypi.org/project/starlette/ |
| Pydantic | ≥2.9.0（未锁版本） | Copyright (c) 2017 to present Pydantic Services Inc. and individual contributors. | MIT | https://github.com/pydantic/pydantic |
| click | ≥7.0（未锁版本） | 待补充（https://github.com/pallets/click/blob/main/LICENSE.txt） | BSD-3-Clause | https://pypi.org/project/click/ |
| h11 | ≥0.8（未锁版本） | 待补充（https://github.com/python-hyper/h11/blob/main/LICENSE.txt） | MIT | https://pypi.org/project/h11/ |
| typing-extensions / typing-inspection / annotated-doc | 未锁 | 待补充 | UNKNOWN（未核对） | 见依赖清单 §1 |

## 2. 内嵌解释器与构建工具链

| 组件 | 版本 | 版权 | 许可证 | 来源 |
|---|---|---|---|---|
| Python（冻结产物内嵌解释器） | 3.14.6 | Copyright © 2001 Python Software Foundation; All Rights Reserved. | PSF-2.0 | https://docs.python.org/3/license.html |

- PSF-2.0 分发条件（协议 §2/§3 摘要）：保留 PSF LICENSE AGREEMENT 与
  版权声明；衍生版本须附对 Python 所做更改的简要说明。Fathom 冻结
  helper 未修改 CPython 源码，发行说明中按此声明。
- PyInstaller（6.22.3，构建工具，其 bootloader 随冻结产物分发）为
  GPLv2-or-later + 例外条款（允许构建并分发非自由/商用程序，PyPI license
  字段原文见依赖清单 §2）；Fathom 依该例外分发，不因此改变自身许可证。

## 3. Rust / Tauri crate

以下组件按 Cargo.lock（基线 `f035d92`）随桌面壳编译链接分发。许可证
均经 crates.io 元数据核对（见依赖清单 §3 表）；版权行未逐一摘录，统一
**待补充**（以各 crate 来源仓库为准）：

| crate | 锁定版本 | 许可证 | 来源 |
|---|---|---|---|
| tauri | 2.11.5 | Apache-2.0 OR MIT | https://crates.io/crates/tauri/2.11.5 |
| tauri-build | 2.6.3 | Apache-2.0 OR MIT | https://crates.io/crates/tauri-build/2.6.3 |
| tauri-plugin-opener | 2.5.5 | Apache-2.0 OR MIT | https://crates.io/crates/tauri-plugin-opener/2.5.5 |
| serde | 1.0.229 | MIT OR Apache-2.0 | https://crates.io/crates/serde/1.0.229 |
| serde_json | 1.0.151 | MIT OR Apache-2.0 | https://crates.io/crates/serde_json/1.0.151 |
| wry | 0.55.1 | Apache-2.0 OR MIT | https://crates.io/crates/wry/0.55.1 |
| tao | 0.35.3 | Apache-2.0 | https://crates.io/crates/tao/0.35.3 |
| tokio | 1.53.1 | MIT | https://crates.io/crates/tokio/1.53.1 |
| objc2 | 0.6.4 | MIT | https://crates.io/crates/objc2/0.6.4 |
| log | 0.4.34 | MIT OR Apache-2.0 | https://crates.io/crates/log/0.4.34 |
| thiserror | 1.0.69 / 2.0.20 | MIT OR Apache-2.0 | https://crates.io/crates/thiserror |
| libc | 0.2.189 | MIT OR Apache-2.0 | https://crates.io/crates/libc/0.2.189 |
| getrandom | 0.2.17 / 0.3.4 / 0.4.3 | MIT OR Apache-2.0 | https://crates.io/crates/getrandom |
| semver | 1.0.28 | MIT OR Apache-2.0 | https://crates.io/crates/semver/1.0.28 |
| http | 1.5.0 | MIT OR Apache-2.0 | https://crates.io/crates/http/1.5.0 |
| url | 2.5.8 | MIT OR Apache-2.0 | https://crates.io/crates/url/2.5.8 |
| ico | 0.5.0 | MIT | https://crates.io/crates/ico/0.5.0 |
| png | 0.17.16 / 0.18.1 | MIT OR Apache-2.0 | https://crates.io/crates/png |
| base64（及 Cargo.lock 其余传递 crate） | 多版本 | UNKNOWN | 未核对；完整清单以 Cargo.lock 为准 |

## 4. 前端 vendored 资源

**ECharts**（frontend/vendor/echarts.min.js，文件内核心 export 标识
5.6.0）——Apache License 2.0。文件头自带 ASF 许可证声明（原文随文件
分发，未删改）：

> Licensed to the Apache Software Foundation (ASF) under one
> or more contributor license agreements. See the NOTICE file
> distributed with this work for additional information
> regarding copyright ownership. The ASF licenses this file
> to you under the Apache License, Version 2.0 (the
> "License"); you may not use this file except in compliance
> with the License. You may obtain a copy of the License at
>
>   http://www.apache.org/licenses/LICENSE-2.0

（完整许可证文本：https://www.apache.org/licenses/LICENSE-2.0.txt；
上游项目 https://echarts.apache.org 。上游精确 dist 版本与 SHA-256 核对
状态见依赖清单 §4。）

**内嵌 tslib**（Microsoft 版权段，随 ECharts bundle；npm 元数据 tslib
2.3.0）——0BSD。文件头版权段原文随文件分发：

> Copyright (c) Microsoft Corporation.
>
> Permission to use, copy, modify, and/or distribute this software for any
> purpose with or without fee is hereby granted.

**内嵌 zrender**（随 ECharts bundle）——待补充（来源
https://github.com/ecomfe/zrender ；随 echarts 官方组合分发，未单独核对）。

## 5. 图形资源

- frontend/icons.js SVG 图标集、apps/desktop/src-tauri/icons/tray.png
  （scripts/make_tray_icon.py 生成）：项目原创，无第三方权利。
- apps/desktop/src-tauri/icons/icon.png：来源未记录（疑似 Tauri 脚手架
  占位），**待 ISS-045 以正式图标替换后方可作为发行资产**。
