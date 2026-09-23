# Third-Party Notices

Fathom 自身采用 Apache License 2.0（见仓库根 `LICENSE`，Copyright 2026 maoking）。

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

| 组件 | 锁定版本 | 许可证 | 来源 | 类别 | 发行影响 |
| --- | --- | --- | --- | --- | --- |
| annotated-doc | 0.0.5 | MIT | https://pypi.org/project/annotated-doc/0.0.5/ | 构建 |  |
| annotated-types | 0.8.0 | MIT | https://pypi.org/project/annotated-types/0.8.0/ | 构建 |  |
| anyio | 4.15.1 | MIT | https://pypi.org/project/anyio/4.15.1/ | 构建 |  |
| certifi | 2026.7.22 | MPL-2.0 | https://pypi.org/project/certifi/2026.7.22/ | 构建 |  |
| click | 8.5.0 | BSD-3-Clause | https://pypi.org/project/click/8.5.0/ | 构建 |  |
| fastapi | 0.141.1 | MIT | https://pypi.org/project/fastapi/0.141.1/ | 构建 |  |
| h11 | 0.16.0 | MIT | https://pypi.org/project/h11/0.16.0/ | 构建 |  |
| httpcore | 1.0.9 | BSD-3-Clause | https://pypi.org/project/httpcore/1.0.9/ | 构建 |  |
| httpx | 0.28.1 | BSD-3-Clause | https://pypi.org/project/httpx/0.28.1/ | 构建 |  |
| idna | 3.19 | BSD-3-Clause | https://pypi.org/project/idna/3.19/ | 构建 |  |
| pydantic | 2.13.5 | MIT | https://pypi.org/project/pydantic/2.13.5/ | 构建 |  |
| pydantic-core | 2.46.5 | MIT | https://pypi.org/project/pydantic-core/2.46.5/ | 构建 |  |
| pytest | 9.1.1 | MIT | https://pypi.org/project/pytest/9.1.1/ | 构建 |  |
| starlette | 1.6.0 | BSD-3-Clause | https://pypi.org/project/starlette/1.6.0/ | 构建 |  |
| typing-extensions | 4.16.0 | PSF-2.0 | https://pypi.org/project/typing-extensions/4.16.0/ | 构建 |  |
| typing-inspection | 0.4.4 | MIT | https://pypi.org/project/typing-inspection/0.4.4/ | 构建 |  |
| uvicorn | 0.52.4 | BSD-3-Clause | https://pypi.org/project/uvicorn/0.52.4/ | 构建 |  |
| iniconfig | 2.3.0 | MIT | https://pypi.org/project/iniconfig/2.3.0/ | 测试 |  |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause | https://pypi.org/project/packaging/26.3/ | 测试 |  |
| pluggy | 1.6.0 | MIT | https://pypi.org/project/pluggy/1.6.0/ | 测试 |  |
| pygments | 2.21.0 | BSD-2-Clause | https://pypi.org/project/pygments/2.21.0/ | 测试 |  |

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

| crate | 锁定版本 | 许可证 | 来源 | 发行影响 |
| --- | --- | --- | --- | --- |
| **fathom-desktop**（工作区自有） | 0.3.0 | Apache-2.0 | apps/desktop/src-tauri/Cargo.toml | — |
| adler2 | 2.0.1 | 0BSD OR MIT OR Apache-2.0 | https://crates.io/crates/adler2/2.0.1 |  |
| aho-corasick | 1.1.5 | Unlicense OR MIT | https://crates.io/crates/aho-corasick/1.1.5 |  |
| alloc-no-stdlib | 2.0.4 | BSD-3-Clause | https://crates.io/crates/alloc-no-stdlib/2.0.4 |  |
| alloc-stdlib | 0.2.4 | BSD-3-Clause | https://crates.io/crates/alloc-stdlib/0.2.4 |  |
| android_system_properties | 0.1.6 | MIT OR Apache-2.0 | https://crates.io/crates/android_system_properties/0.1.6 |  |
| anyhow | 1.0.104 | MIT OR Apache-2.0 | https://crates.io/crates/anyhow/1.0.104 |  |
| arbitrary | 1.4.2 | UNKNOWN | https://crates.io/crates/arbitrary/1.4.2 | 来源待核：本地 registry Cargo.toml 未提供 license 字段（DEC-024 口径：UNKNOWN 保留，不阻断） |
| async-broadcast | 0.7.2 | MIT OR Apache-2.0 | https://crates.io/crates/async-broadcast/0.7.2 |  |
| async-channel | 2.5.0 | Apache-2.0 OR MIT | https://crates.io/crates/async-channel/2.5.0 |  |
| async-executor | 1.14.0 | Apache-2.0 OR MIT | https://crates.io/crates/async-executor/1.14.0 |  |
| async-io | 2.6.0 | Apache-2.0 OR MIT | https://crates.io/crates/async-io/2.6.0 |  |
| async-lock | 3.4.2 | Apache-2.0 OR MIT | https://crates.io/crates/async-lock/3.4.2 |  |
| async-process | 2.5.0 | Apache-2.0 OR MIT | https://crates.io/crates/async-process/2.5.0 |  |
| async-recursion | 1.1.1 | MIT OR Apache-2.0 | https://crates.io/crates/async-recursion/1.1.1 |  |
| async-signal | 0.2.14 | Apache-2.0 OR MIT | https://crates.io/crates/async-signal/0.2.14 |  |
| async-task | 4.7.1 | Apache-2.0 OR MIT | https://crates.io/crates/async-task/4.7.1 |  |
| async-trait | 0.1.92 | MIT OR Apache-2.0 | https://crates.io/crates/async-trait/0.1.92 |  |
| atk | 0.18.2 | MIT | https://crates.io/crates/atk/0.18.2 |  |
| atk-sys | 0.18.2 | MIT | https://crates.io/crates/atk-sys/0.18.2 |  |
| atomic-waker | 1.1.2 | Apache-2.0 OR MIT | https://crates.io/crates/atomic-waker/1.1.2 |  |
| autocfg | 1.5.1 | Apache-2.0 OR MIT | https://crates.io/crates/autocfg/1.5.1 |  |
| base64 | 0.21.7 / 0.22.1 / 0.23.1 | MIT OR Apache-2.0 | https://crates.io/crates/base64/0.23.1 |  |
| bit-set | 0.8.0 | Apache-2.0 OR MIT | https://crates.io/crates/bit-set/0.8.0 |  |
| bit-vec | 0.8.0 | Apache-2.0 OR MIT | https://crates.io/crates/bit-vec/0.8.0 |  |
| bitflags | 1.3.2 / 2.13.2 | MIT/Apache-2.0 / MIT OR Apache-2.0 | https://crates.io/crates/bitflags/2.13.2 |  |
| block-buffer | 0.10.4 | MIT OR Apache-2.0 | https://crates.io/crates/block-buffer/0.10.4 |  |
| block2 | 0.6.2 | MIT | https://crates.io/crates/block2/0.6.2 |  |
| blocking | 1.7.0 | Apache-2.0 OR MIT | https://crates.io/crates/blocking/1.7.0 |  |
| brotli | 8.0.4 | BSD-3-Clause AND MIT | https://crates.io/crates/brotli/8.0.4 |  |
| brotli-decompressor | 5.0.3 | BSD-3-Clause/MIT | https://crates.io/crates/brotli-decompressor/5.0.3 |  |
| bs58 | 0.5.1 | MIT/Apache-2.0 | https://crates.io/crates/bs58/0.5.1 |  |
| bumpalo | 3.20.3 | MIT OR Apache-2.0 | https://crates.io/crates/bumpalo/3.20.3 |  |
| bytemuck | 1.25.2 | Zlib OR Apache-2.0 OR MIT | https://crates.io/crates/bytemuck/1.25.2 |  |
| byteorder | 1.5.0 | Unlicense OR MIT | https://crates.io/crates/byteorder/1.5.0 |  |
| bytes | 1.12.1 | MIT | https://crates.io/crates/bytes/1.12.1 |  |
| cairo-rs | 0.18.5 | MIT | https://crates.io/crates/cairo-rs/0.18.5 |  |
| cairo-sys-rs | 0.18.2 | MIT | https://crates.io/crates/cairo-sys-rs/0.18.2 |  |
| camino | 1.2.5 | MIT OR Apache-2.0 | https://crates.io/crates/camino/1.2.5 |  |
| cargo-platform | 0.1.9 | MIT OR Apache-2.0 | https://crates.io/crates/cargo-platform/0.1.9 |  |
| cargo_metadata | 0.19.2 | MIT | https://crates.io/crates/cargo_metadata/0.19.2 |  |
| cargo_toml | 0.22.3 | Apache-2.0 OR MIT | https://crates.io/crates/cargo_toml/0.22.3 |  |
| cc | 1.4.5 | MIT OR Apache-2.0 | https://crates.io/crates/cc/1.4.5 |  |
| cesu8 | 1.1.0 | Apache-2.0/MIT | https://crates.io/crates/cesu8/1.1.0 |  |
| cfb | 0.7.3 | MIT | https://crates.io/crates/cfb/0.7.3 |  |
| cfg-expr | 0.15.8 | MIT OR Apache-2.0 | https://crates.io/crates/cfg-expr/0.15.8 |  |
| cfg-if | 1.0.4 | MIT OR Apache-2.0 | https://crates.io/crates/cfg-if/1.0.4 |  |
| chrono | 0.4.45 | MIT OR Apache-2.0 | https://crates.io/crates/chrono/0.4.45 |  |
| combine | 4.6.8 | MIT | https://crates.io/crates/combine/4.6.8 |  |
| concurrent-queue | 2.5.0 | Apache-2.0 OR MIT | https://crates.io/crates/concurrent-queue/2.5.0 |  |
| cookie | 0.18.2 | MIT OR Apache-2.0 | https://crates.io/crates/cookie/0.18.2 |  |
| core-foundation | 0.10.1 | MIT OR Apache-2.0 | https://crates.io/crates/core-foundation/0.10.1 |  |
| core-foundation-sys | 0.8.7 | MIT OR Apache-2.0 | https://crates.io/crates/core-foundation-sys/0.8.7 |  |
| core-graphics | 0.25.0 | MIT OR Apache-2.0 | https://crates.io/crates/core-graphics/0.25.0 |  |
| core-graphics-types | 0.2.0 | MIT OR Apache-2.0 | https://crates.io/crates/core-graphics-types/0.2.0 |  |
| cpufeatures | 0.2.17 | MIT OR Apache-2.0 | https://crates.io/crates/cpufeatures/0.2.17 |  |
| crc32fast | 1.5.1 | MIT OR Apache-2.0 | https://crates.io/crates/crc32fast/1.5.1 |  |
| crossbeam-channel | 0.5.17 | MIT OR Apache-2.0 | https://crates.io/crates/crossbeam-channel/0.5.17 |  |
| crossbeam-utils | 0.8.23 | MIT OR Apache-2.0 | https://crates.io/crates/crossbeam-utils/0.8.23 |  |
| crypto-common | 0.1.7 | MIT OR Apache-2.0 | https://crates.io/crates/crypto-common/0.1.7 |  |
| cssparser | 0.36.0 | MPL-2.0 | https://crates.io/crates/cssparser/0.36.0 |  |
| cssparser-macros | 0.6.1 | MPL-2.0 | https://crates.io/crates/cssparser-macros/0.6.1 |  |
| ctor | 0.8.0 | Apache-2.0 OR MIT | https://crates.io/crates/ctor/0.8.0 |  |
| ctor-proc-macro | 0.0.7 | Apache-2.0 OR MIT | https://crates.io/crates/ctor-proc-macro/0.0.7 |  |
| darling | 0.24.1 | MIT | https://crates.io/crates/darling/0.24.1 |  |
| darling_core | 0.24.1 | MIT | https://crates.io/crates/darling_core/0.24.1 |  |
| darling_macro | 0.24.1 | MIT | https://crates.io/crates/darling_macro/0.24.1 |  |
| dbus | 0.9.12 | Apache-2.0/MIT | https://crates.io/crates/dbus/0.9.12 |  |
| defmt | 1.1.1 | MIT OR Apache-2.0 | https://crates.io/crates/defmt/1.1.1 |  |
| defmt-macros | 1.1.1 | MIT OR Apache-2.0 | https://crates.io/crates/defmt-macros/1.1.1 |  |
| defmt-parser | 1.0.0 | MIT OR Apache-2.0 | https://crates.io/crates/defmt-parser/1.0.0 |  |
| deranged | 0.5.8 | MIT OR Apache-2.0 | https://crates.io/crates/deranged/0.5.8 |  |
| derive_arbitrary | 1.4.2 | UNKNOWN | https://crates.io/crates/derive_arbitrary/1.4.2 | 来源待核：本地 registry Cargo.toml 未提供 license 字段（DEC-024 口径：UNKNOWN 保留，不阻断） |
| derive_more | 2.1.1 | MIT | https://crates.io/crates/derive_more/2.1.1 |  |
| derive_more-impl | 2.1.1 | MIT | https://crates.io/crates/derive_more-impl/2.1.1 |  |
| digest | 0.10.7 | MIT OR Apache-2.0 | https://crates.io/crates/digest/0.10.7 |  |
| dirs | 6.0.0 | MIT OR Apache-2.0 | https://crates.io/crates/dirs/6.0.0 |  |
| dirs-sys | 0.5.0 | MIT OR Apache-2.0 | https://crates.io/crates/dirs-sys/0.5.0 |  |
| dispatch2 | 0.3.1 | Zlib OR Apache-2.0 OR MIT | https://crates.io/crates/dispatch2/0.3.1 |  |
| displaydoc | 0.2.7 | MIT OR Apache-2.0 | https://crates.io/crates/displaydoc/0.2.7 |  |
| dlopen2 | 0.8.2 | MIT | https://crates.io/crates/dlopen2/0.8.2 |  |
| dlopen2_derive | 0.4.3 | MIT | https://crates.io/crates/dlopen2_derive/0.4.3 |  |
| dom_query | 0.27.0 | MIT | https://crates.io/crates/dom_query/0.27.0 |  |
| dpi | 0.1.2 | Apache-2.0 AND MIT | https://crates.io/crates/dpi/0.1.2 |  |
| dtoa | 1.0.11 | MIT OR Apache-2.0 | https://crates.io/crates/dtoa/1.0.11 |  |
| dtoa-short | 0.3.5 | MPL-2.0 | https://crates.io/crates/dtoa-short/0.3.5 |  |
| dtor | 0.3.0 | Apache-2.0 OR MIT | https://crates.io/crates/dtor/0.3.0 |  |
| dtor-proc-macro | 0.0.6 | Apache-2.0 OR MIT | https://crates.io/crates/dtor-proc-macro/0.0.6 |  |
| dunce | 1.0.5 | CC0-1.0 OR MIT-0 OR Apache-2.0 | https://crates.io/crates/dunce/1.0.5 |  |
| dyn-clone | 1.0.20 | MIT OR Apache-2.0 | https://crates.io/crates/dyn-clone/1.0.20 |  |
| embed-resource | 3.0.11 | MIT | https://crates.io/crates/embed-resource/3.0.11 |  |
| embed_plist | 1.2.2 | MIT OR Apache-2.0 | https://crates.io/crates/embed_plist/1.2.2 |  |
| endi | 1.1.1 | MIT | https://crates.io/crates/endi/1.1.1 |  |
| enumflags2 | 0.7.12 | MIT OR Apache-2.0 | https://crates.io/crates/enumflags2/0.7.12 |  |
| enumflags2_derive | 0.7.12 | MIT OR Apache-2.0 | https://crates.io/crates/enumflags2_derive/0.7.12 |  |
| equivalent | 1.0.2 | Apache-2.0 OR MIT | https://crates.io/crates/equivalent/1.0.2 |  |
| erased-serde | 0.4.10 | MIT OR Apache-2.0 | https://crates.io/crates/erased-serde/0.4.10 |  |
| errno | 0.3.14 | MIT OR Apache-2.0 | https://crates.io/crates/errno/0.3.14 |  |
| event-listener | 5.4.2 | Apache-2.0 OR MIT | https://crates.io/crates/event-listener/5.4.2 |  |
| event-listener-strategy | 0.5.4 | Apache-2.0 OR MIT | https://crates.io/crates/event-listener-strategy/0.5.4 |  |
| fastrand | 2.5.0 | Apache-2.0 OR MIT | https://crates.io/crates/fastrand/2.5.0 |  |
| fdeflate | 0.3.7 | MIT OR Apache-2.0 | https://crates.io/crates/fdeflate/0.3.7 |  |
| field-offset | 0.3.6 | MIT OR Apache-2.0 | https://crates.io/crates/field-offset/0.3.6 |  |
| filetime | 0.2.29 | MIT/Apache-2.0 | https://crates.io/crates/filetime/0.2.29 |  |
| find-msvc-tools | 0.1.12 | MIT OR Apache-2.0 | https://crates.io/crates/find-msvc-tools/0.1.12 |  |
| flate2 | 1.1.10 | MIT OR Apache-2.0 | https://crates.io/crates/flate2/1.1.10 |  |
| fnv | 1.0.7 | Apache-2.0 / MIT | https://crates.io/crates/fnv/1.0.7 |  |
| foldhash | 0.2.0 | Zlib | https://crates.io/crates/foldhash/0.2.0 |  |
| foreign-types | 0.5.0 | MIT/Apache-2.0 | https://crates.io/crates/foreign-types/0.5.0 |  |
| foreign-types-macros | 0.2.4 | MIT/Apache-2.0 | https://crates.io/crates/foreign-types-macros/0.2.4 |  |
| foreign-types-shared | 0.3.1 | MIT/Apache-2.0 | https://crates.io/crates/foreign-types-shared/0.3.1 |  |
| form_urlencoded | 1.2.2 | MIT OR Apache-2.0 | https://crates.io/crates/form_urlencoded/1.2.2 |  |
| futures-channel | 0.3.34 | MIT OR Apache-2.0 | https://crates.io/crates/futures-channel/0.3.34 |  |
| futures-core | 0.3.34 | MIT OR Apache-2.0 | https://crates.io/crates/futures-core/0.3.34 |  |
| futures-executor | 0.3.34 | MIT OR Apache-2.0 | https://crates.io/crates/futures-executor/0.3.34 |  |
| futures-io | 0.3.34 | MIT OR Apache-2.0 | https://crates.io/crates/futures-io/0.3.34 |  |
| futures-lite | 2.6.1 | Apache-2.0 OR MIT | https://crates.io/crates/futures-lite/2.6.1 |  |
| futures-macro | 0.3.34 | MIT OR Apache-2.0 | https://crates.io/crates/futures-macro/0.3.34 |  |
| futures-sink | 0.3.34 | MIT OR Apache-2.0 | https://crates.io/crates/futures-sink/0.3.34 |  |
| futures-task | 0.3.34 | MIT OR Apache-2.0 | https://crates.io/crates/futures-task/0.3.34 |  |
| futures-util | 0.3.34 | MIT OR Apache-2.0 | https://crates.io/crates/futures-util/0.3.34 |  |
| gdk | 0.18.2 | MIT | https://crates.io/crates/gdk/0.18.2 |  |
| gdk-pixbuf | 0.18.5 | MIT | https://crates.io/crates/gdk-pixbuf/0.18.5 |  |
| gdk-pixbuf-sys | 0.18.0 | MIT | https://crates.io/crates/gdk-pixbuf-sys/0.18.0 |  |
| gdk-sys | 0.18.2 | MIT | https://crates.io/crates/gdk-sys/0.18.2 |  |
| gdkwayland-sys | 0.18.2 | MIT | https://crates.io/crates/gdkwayland-sys/0.18.2 |  |
| gdkx11 | 0.18.2 | MIT | https://crates.io/crates/gdkx11/0.18.2 |  |
| gdkx11-sys | 0.18.2 | MIT | https://crates.io/crates/gdkx11-sys/0.18.2 |  |
| generic-array | 0.14.7 | MIT | https://crates.io/crates/generic-array/0.14.7 |  |
| getrandom | 0.2.17 / 0.3.4 / 0.4.3 | MIT OR Apache-2.0 | https://crates.io/crates/getrandom/0.4.3 |  |
| gio | 0.18.4 | MIT | https://crates.io/crates/gio/0.18.4 |  |
| gio-sys | 0.18.1 | MIT | https://crates.io/crates/gio-sys/0.18.1 |  |
| glib | 0.18.5 | MIT | https://crates.io/crates/glib/0.18.5 |  |
| glib-macros | 0.18.5 | MIT | https://crates.io/crates/glib-macros/0.18.5 |  |
| glib-sys | 0.18.1 | MIT | https://crates.io/crates/glib-sys/0.18.1 |  |
| glob | 0.3.4 | MIT OR Apache-2.0 | https://crates.io/crates/glob/0.3.4 |  |
| gobject-sys | 0.18.0 | MIT | https://crates.io/crates/gobject-sys/0.18.0 |  |
| gtk | 0.18.2 | MIT | https://crates.io/crates/gtk/0.18.2 |  |
| gtk-sys | 0.18.2 | MIT | https://crates.io/crates/gtk-sys/0.18.2 |  |
| gtk3-macros | 0.18.2 | MIT | https://crates.io/crates/gtk3-macros/0.18.2 |  |
| hashbrown | 0.12.3 / 0.17.1 | MIT OR Apache-2.0 | https://crates.io/crates/hashbrown/0.17.1 |  |
| heck | 0.4.1 / 0.5.0 | MIT OR Apache-2.0 | https://crates.io/crates/heck/0.5.0 |  |
| hermit-abi | 0.5.3 | MIT OR Apache-2.0 | https://crates.io/crates/hermit-abi/0.5.3 |  |
| hex | 0.4.3 | MIT OR Apache-2.0 | https://crates.io/crates/hex/0.4.3 |  |
| html5ever | 0.38.0 | MIT OR Apache-2.0 | https://crates.io/crates/html5ever/0.38.0 |  |
| http | 1.5.0 | MIT OR Apache-2.0 | https://crates.io/crates/http/1.5.0 |  |
| http-body | 1.1.0 | MIT | https://crates.io/crates/http-body/1.1.0 |  |
| http-body-util | 0.1.5 | MIT | https://crates.io/crates/http-body-util/0.1.5 |  |
| httparse | 1.10.1 | MIT OR Apache-2.0 | https://crates.io/crates/httparse/1.10.1 |  |
| hyper | 1.11.1 | MIT | https://crates.io/crates/hyper/1.11.1 |  |
| hyper-rustls | 0.27.9 | Apache-2.0 OR ISC OR MIT | https://crates.io/crates/hyper-rustls/0.27.9 |  |
| hyper-util | 0.1.20 | MIT | https://crates.io/crates/hyper-util/0.1.20 |  |
| iana-time-zone | 0.1.65 | MIT OR Apache-2.0 | https://crates.io/crates/iana-time-zone/0.1.65 |  |
| iana-time-zone-haiku | 0.1.2 | MIT OR Apache-2.0 | https://crates.io/crates/iana-time-zone-haiku/0.1.2 |  |
| ico | 0.5.0 | MIT | https://crates.io/crates/ico/0.5.0 |  |
| icu_collections | 2.3.0 | Unicode-3.0 | https://crates.io/crates/icu_collections/2.3.0 |  |
| icu_locale_core | 2.3.0 | Unicode-3.0 | https://crates.io/crates/icu_locale_core/2.3.0 |  |
| icu_normalizer | 2.3.0 | Unicode-3.0 | https://crates.io/crates/icu_normalizer/2.3.0 |  |
| icu_normalizer_data | 2.3.0 | Unicode-3.0 | https://crates.io/crates/icu_normalizer_data/2.3.0 |  |
| icu_properties | 2.3.0 | Unicode-3.0 | https://crates.io/crates/icu_properties/2.3.0 |  |
| icu_properties_data | 2.3.0 | Unicode-3.0 | https://crates.io/crates/icu_properties_data/2.3.0 |  |
| icu_provider | 2.3.1 | Unicode-3.0 | https://crates.io/crates/icu_provider/2.3.1 |  |
| ident_case | 1.0.1 | MIT/Apache-2.0 | https://crates.io/crates/ident_case/1.0.1 |  |
| idna | 1.1.0 | MIT OR Apache-2.0 | https://crates.io/crates/idna/1.1.0 |  |
| idna_adapter | 1.2.2 | Apache-2.0 OR MIT | https://crates.io/crates/idna_adapter/1.2.2 |  |
| indexmap | 1.9.3 / 2.14.2 | Apache-2.0 OR MIT | https://crates.io/crates/indexmap/2.14.2 |  |
| infer | 0.19.0 | MIT | https://crates.io/crates/infer/0.19.0 |  |
| ipnet | 2.12.2 | MIT OR Apache-2.0 | https://crates.io/crates/ipnet/2.12.2 |  |
| is-docker | 0.2.0 | MIT | https://crates.io/crates/is-docker/0.2.0 |  |
| is-wsl | 0.4.0 | MIT | https://crates.io/crates/is-wsl/0.4.0 |  |
| itoa | 1.0.18 | MIT OR Apache-2.0 | https://crates.io/crates/itoa/1.0.18 |  |
| javascriptcore-rs | 1.1.2 | MIT | https://crates.io/crates/javascriptcore-rs/1.1.2 |  |
| javascriptcore-rs-sys | 1.1.1 | MIT | https://crates.io/crates/javascriptcore-rs-sys/1.1.1 |  |
| jiff | 0.2.35 | Unlicense OR MIT | https://crates.io/crates/jiff/0.2.35 |  |
| jiff-core | 0.1.0 | Unlicense OR MIT | https://crates.io/crates/jiff-core/0.1.0 |  |
| jiff-static | 0.2.35 | Unlicense OR MIT | https://crates.io/crates/jiff-static/0.2.35 |  |
| jiff-tzdb | 0.1.8 | Unlicense OR MIT | https://crates.io/crates/jiff-tzdb/0.1.8 |  |
| jiff-tzdb-platform | 0.1.3 | Unlicense OR MIT | https://crates.io/crates/jiff-tzdb-platform/0.1.3 |  |
| jni | 0.21.1 / 0.22.4 | MIT/Apache-2.0 / UNKNOWN | https://crates.io/crates/jni/0.22.4 | 来源待核：本地 registry Cargo.toml 未提供 license 字段（DEC-024 口径：UNKNOWN 保留，不阻断） |
| jni-macros | 0.22.4 | UNKNOWN | https://crates.io/crates/jni-macros/0.22.4 | 来源待核：本地 registry Cargo.toml 未提供 license 字段（DEC-024 口径：UNKNOWN 保留，不阻断） |
| jni-sys | 0.3.1 / 0.4.1 | MIT OR Apache-2.0 | https://crates.io/crates/jni-sys/0.4.1 |  |
| jni-sys-macros | 0.4.1 | MIT OR Apache-2.0 | https://crates.io/crates/jni-sys-macros/0.4.1 |  |
| js-sys | 0.3.105 | MIT OR Apache-2.0 | https://crates.io/crates/js-sys/0.3.105 |  |
| json-patch | 3.0.1 | MIT/Apache-2.0 | https://crates.io/crates/json-patch/3.0.1 |  |
| jsonptr | 0.6.3 | MIT OR Apache-2.0 | https://crates.io/crates/jsonptr/0.6.3 |  |
| keyboard-types | 0.7.0 | MIT OR Apache-2.0 | https://crates.io/crates/keyboard-types/0.7.0 |  |
| libappindicator | 0.9.0 | Apache-2.0 OR MIT | https://crates.io/crates/libappindicator/0.9.0 |  |
| libappindicator-sys | 0.9.0 | Apache-2.0 OR MIT | https://crates.io/crates/libappindicator-sys/0.9.0 |  |
| libc | 0.2.189 | MIT OR Apache-2.0 | https://crates.io/crates/libc/0.2.189 |  |
| libdbus-sys | 0.2.7 | Apache-2.0/MIT | https://crates.io/crates/libdbus-sys/0.2.7 |  |
| libloading | 0.7.4 | ISC | https://crates.io/crates/libloading/0.7.4 |  |
| libredox | 0.1.24 | MIT | https://crates.io/crates/libredox/0.1.24 |  |
| linux-raw-sys | 0.12.1 | Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT | https://crates.io/crates/linux-raw-sys/0.12.1 |  |
| litemap | 0.8.3 | Unicode-3.0 | https://crates.io/crates/litemap/0.8.3 |  |
| lock_api | 0.4.14 | MIT OR Apache-2.0 | https://crates.io/crates/lock_api/0.4.14 |  |
| log | 0.4.34 | MIT OR Apache-2.0 | https://crates.io/crates/log/0.4.34 |  |
| markup5ever | 0.38.0 | MIT OR Apache-2.0 | https://crates.io/crates/markup5ever/0.38.0 |  |
| memchr | 2.8.3 | Unlicense OR MIT | https://crates.io/crates/memchr/2.8.3 |  |
| memoffset | 0.9.1 | MIT | https://crates.io/crates/memoffset/0.9.1 |  |
| mime | 0.3.17 | MIT OR Apache-2.0 | https://crates.io/crates/mime/0.3.17 |  |
| minisign-verify | 0.2.5 | MIT | https://crates.io/crates/minisign-verify/0.2.5 |  |
| miniz_oxide | 0.8.9 / 0.9.1 | MIT OR Zlib OR Apache-2.0 | https://crates.io/crates/miniz_oxide/0.9.1 |  |
| mio | 1.2.3 | MIT | https://crates.io/crates/mio/1.2.3 |  |
| muda | 0.19.3 | Apache-2.0 OR MIT | https://crates.io/crates/muda/0.19.3 |  |
| ndk | 0.9.0 | MIT OR Apache-2.0 | https://crates.io/crates/ndk/0.9.0 |  |
| ndk-sys | 0.6.0+11769913 | MIT OR Apache-2.0 | https://crates.io/crates/ndk-sys/0.6.0+11769913 |  |
| new_debug_unreachable | 1.0.6 | MIT | https://crates.io/crates/new_debug_unreachable/1.0.6 |  |
| num-conv | 0.2.2 | MIT OR Apache-2.0 | https://crates.io/crates/num-conv/0.2.2 |  |
| num-traits | 0.2.19 | MIT OR Apache-2.0 | https://crates.io/crates/num-traits/0.2.19 |  |
| num_enum | 0.7.6 | BSD-3-Clause OR MIT OR Apache-2.0 | https://crates.io/crates/num_enum/0.7.6 |  |
| num_enum_derive | 0.7.6 | BSD-3-Clause OR MIT OR Apache-2.0 | https://crates.io/crates/num_enum_derive/0.7.6 |  |
| objc2 | 0.6.4 | MIT | https://crates.io/crates/objc2/0.6.4 |  |
| objc2-app-kit | 0.3.2 | Zlib OR Apache-2.0 OR MIT | https://crates.io/crates/objc2-app-kit/0.3.2 |  |
| objc2-cloud-kit | 0.3.2 | Zlib OR Apache-2.0 OR MIT | https://crates.io/crates/objc2-cloud-kit/0.3.2 |  |
| objc2-core-data | 0.3.2 | Zlib OR Apache-2.0 OR MIT | https://crates.io/crates/objc2-core-data/0.3.2 |  |
| objc2-core-foundation | 0.3.2 | Zlib OR Apache-2.0 OR MIT | https://crates.io/crates/objc2-core-foundation/0.3.2 |  |
| objc2-core-graphics | 0.3.2 | Zlib OR Apache-2.0 OR MIT | https://crates.io/crates/objc2-core-graphics/0.3.2 |  |
| objc2-core-image | 0.3.2 | Zlib OR Apache-2.0 OR MIT | https://crates.io/crates/objc2-core-image/0.3.2 |  |
| objc2-core-location | 0.3.2 | Zlib OR Apache-2.0 OR MIT | https://crates.io/crates/objc2-core-location/0.3.2 |  |
| objc2-core-text | 0.3.2 | Zlib OR Apache-2.0 OR MIT | https://crates.io/crates/objc2-core-text/0.3.2 |  |
| objc2-encode | 4.1.0 | MIT | https://crates.io/crates/objc2-encode/4.1.0 |  |
| objc2-exception-helper | 0.1.1 | Zlib OR Apache-2.0 OR MIT | https://crates.io/crates/objc2-exception-helper/0.1.1 |  |
| objc2-foundation | 0.3.2 | MIT | https://crates.io/crates/objc2-foundation/0.3.2 |  |
| objc2-io-surface | 0.3.2 | Zlib OR Apache-2.0 OR MIT | https://crates.io/crates/objc2-io-surface/0.3.2 |  |
| objc2-osa-kit | 0.3.2 | Zlib OR Apache-2.0 OR MIT | https://crates.io/crates/objc2-osa-kit/0.3.2 |  |
| objc2-quartz-core | 0.3.2 | Zlib OR Apache-2.0 OR MIT | https://crates.io/crates/objc2-quartz-core/0.3.2 |  |
| objc2-ui-kit | 0.3.2 | Zlib OR Apache-2.0 OR MIT | https://crates.io/crates/objc2-ui-kit/0.3.2 |  |
| objc2-user-notifications | 0.3.2 | Zlib OR Apache-2.0 OR MIT | https://crates.io/crates/objc2-user-notifications/0.3.2 |  |
| objc2-web-kit | 0.3.2 | Zlib OR Apache-2.0 OR MIT | https://crates.io/crates/objc2-web-kit/0.3.2 |  |
| once_cell | 1.21.4 | MIT OR Apache-2.0 | https://crates.io/crates/once_cell/1.21.4 |  |
| open | 5.4.4 | MIT | https://crates.io/crates/open/5.4.4 |  |
| openssl-probe | 0.2.1 | UNKNOWN | https://crates.io/crates/openssl-probe/0.2.1 | 来源待核：本地 registry Cargo.toml 未提供 license 字段（DEC-024 口径：UNKNOWN 保留，不阻断） |
| option-ext | 0.2.0 | MPL-2.0 | https://crates.io/crates/option-ext/0.2.0 |  |
| ordered-stream | 0.2.0 | MIT OR Apache-2.0 | https://crates.io/crates/ordered-stream/0.2.0 |  |
| osakit | 0.3.1 | MIT OR Apache-2.0 | https://crates.io/crates/osakit/0.3.1 |  |
| pango | 0.18.3 | MIT | https://crates.io/crates/pango/0.18.3 |  |
| pango-sys | 0.18.0 | MIT | https://crates.io/crates/pango-sys/0.18.0 |  |
| parking | 2.2.1 | Apache-2.0 OR MIT | https://crates.io/crates/parking/2.2.1 |  |
| parking_lot | 0.12.5 | MIT OR Apache-2.0 | https://crates.io/crates/parking_lot/0.12.5 |  |
| parking_lot_core | 0.9.12 | MIT OR Apache-2.0 | https://crates.io/crates/parking_lot_core/0.9.12 |  |
| percent-encoding | 2.3.2 | MIT OR Apache-2.0 | https://crates.io/crates/percent-encoding/2.3.2 |  |
| phf | 0.13.1 | MIT | https://crates.io/crates/phf/0.13.1 |  |
| phf_codegen | 0.13.1 | MIT | https://crates.io/crates/phf_codegen/0.13.1 |  |
| phf_generator | 0.13.1 | MIT | https://crates.io/crates/phf_generator/0.13.1 |  |
| phf_macros | 0.13.1 | MIT | https://crates.io/crates/phf_macros/0.13.1 |  |
| phf_shared | 0.13.1 | MIT | https://crates.io/crates/phf_shared/0.13.1 |  |
| pin-project-lite | 0.2.17 | Apache-2.0 OR MIT | https://crates.io/crates/pin-project-lite/0.2.17 |  |
| piper | 0.2.5 | MIT OR Apache-2.0 | https://crates.io/crates/piper/0.2.5 |  |
| pkg-config | 0.3.34 | MIT OR Apache-2.0 | https://crates.io/crates/pkg-config/0.3.34 |  |
| plist | 1.10.1 | MIT | https://crates.io/crates/plist/1.10.1 |  |
| png | 0.17.16 / 0.18.1 | MIT OR Apache-2.0 | https://crates.io/crates/png/0.18.1 |  |
| polling | 3.11.0 | Apache-2.0 OR MIT | https://crates.io/crates/polling/3.11.0 |  |
| portable-atomic | 1.15.0 | Apache-2.0 OR MIT | https://crates.io/crates/portable-atomic/1.15.0 |  |
| portable-atomic-util | 0.2.8 | Apache-2.0 OR MIT | https://crates.io/crates/portable-atomic-util/0.2.8 |  |
| potential_utf | 0.1.6 | Unicode-3.0 | https://crates.io/crates/potential_utf/0.1.6 |  |
| powerfmt | 0.2.0 | MIT OR Apache-2.0 | https://crates.io/crates/powerfmt/0.2.0 |  |
| precomputed-hash | 0.1.1 | MIT | https://crates.io/crates/precomputed-hash/0.1.1 |  |
| proc-macro-crate | 1.3.1 / 2.0.2 / 3.5.0 | MIT OR Apache-2.0 | https://crates.io/crates/proc-macro-crate/3.5.0 |  |
| proc-macro-error | 1.0.4 | MIT OR Apache-2.0 | https://crates.io/crates/proc-macro-error/1.0.4 |  |
| proc-macro-error-attr | 1.0.4 | MIT OR Apache-2.0 | https://crates.io/crates/proc-macro-error-attr/1.0.4 |  |
| proc-macro2 | 1.0.107 | MIT OR Apache-2.0 | https://crates.io/crates/proc-macro2/1.0.107 |  |
| quick-xml | 0.42.0 | MIT | https://crates.io/crates/quick-xml/0.42.0 |  |
| quote | 1.0.47 | MIT OR Apache-2.0 | https://crates.io/crates/quote/1.0.47 |  |
| r-efi | 5.3.0 / 6.0.0 | MIT OR Apache-2.0 OR LGPL-2.1-or-later | https://crates.io/crates/r-efi/6.0.0 |  |
| raw-window-handle | 0.6.2 | MIT OR Apache-2.0 OR Zlib | https://crates.io/crates/raw-window-handle/0.6.2 |  |
| redox_syscall | 0.5.18 | MIT | https://crates.io/crates/redox_syscall/0.5.18 |  |
| redox_users | 0.5.2 | MIT | https://crates.io/crates/redox_users/0.5.2 |  |
| ref-cast | 1.0.27 | MIT OR Apache-2.0 | https://crates.io/crates/ref-cast/1.0.27 |  |
| ref-cast-impl | 1.0.27 | MIT OR Apache-2.0 | https://crates.io/crates/ref-cast-impl/1.0.27 |  |
| regex | 1.13.1 | MIT OR Apache-2.0 | https://crates.io/crates/regex/1.13.1 |  |
| regex-automata | 0.4.18 | MIT OR Apache-2.0 | https://crates.io/crates/regex-automata/0.4.18 |  |
| regex-syntax | 0.8.11 | MIT OR Apache-2.0 | https://crates.io/crates/regex-syntax/0.8.11 |  |
| reqwest | 0.13.5 | MIT OR Apache-2.0 | https://crates.io/crates/reqwest/0.13.5 |  |
| ring | 0.17.14 | Apache-2.0 AND ISC | https://crates.io/crates/ring/0.17.14 |  |
| rustc-hash | 2.1.3 | Apache-2.0 OR MIT | https://crates.io/crates/rustc-hash/2.1.3 |  |
| rustc_version | 0.4.1 | MIT OR Apache-2.0 | https://crates.io/crates/rustc_version/0.4.1 |  |
| rustix | 1.1.4 | Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT | https://crates.io/crates/rustix/1.1.4 |  |
| rustls | 0.23.43 | Apache-2.0 OR ISC OR MIT | https://crates.io/crates/rustls/0.23.43 |  |
| rustls-native-certs | 0.8.4 | UNKNOWN | https://crates.io/crates/rustls-native-certs/0.8.4 | 来源待核：本地 registry Cargo.toml 未提供 license 字段（DEC-024 口径：UNKNOWN 保留，不阻断） |
| rustls-pki-types | 1.15.1 | MIT OR Apache-2.0 | https://crates.io/crates/rustls-pki-types/1.15.1 |  |
| rustls-platform-verifier | 0.7.0 | MIT OR Apache-2.0 | https://crates.io/crates/rustls-platform-verifier/0.7.0 |  |
| rustls-platform-verifier-android | 0.1.1 | UNKNOWN | https://crates.io/crates/rustls-platform-verifier-android/0.1.1 | 来源待核：本地 registry Cargo.toml 未提供 license 字段（DEC-024 口径：UNKNOWN 保留，不阻断） |
| rustls-webpki | 0.103.15 | ISC | https://crates.io/crates/rustls-webpki/0.103.15 |  |
| rustversion | 1.0.23 | MIT OR Apache-2.0 | https://crates.io/crates/rustversion/1.0.23 |  |
| same-file | 1.0.6 | Unlicense/MIT | https://crates.io/crates/same-file/1.0.6 |  |
| schannel | 0.1.29 | UNKNOWN | https://crates.io/crates/schannel/0.1.29 | 来源待核：本地 registry Cargo.toml 未提供 license 字段（DEC-024 口径：UNKNOWN 保留，不阻断） |
| schemars | 0.8.22 / 0.9.0 / 1.2.2 | MIT | https://crates.io/crates/schemars/1.2.2 |  |
| schemars_derive | 0.8.22 | MIT | https://crates.io/crates/schemars_derive/0.8.22 |  |
| scopeguard | 1.2.0 | MIT OR Apache-2.0 | https://crates.io/crates/scopeguard/1.2.0 |  |
| security-framework | 3.7.0 | MIT OR Apache-2.0 | https://crates.io/crates/security-framework/3.7.0 |  |
| security-framework-sys | 2.17.0 | MIT OR Apache-2.0 | https://crates.io/crates/security-framework-sys/2.17.0 |  |
| selectors | 0.36.1 | MPL-2.0 | https://crates.io/crates/selectors/0.36.1 |  |
| semver | 1.0.28 | MIT OR Apache-2.0 | https://crates.io/crates/semver/1.0.28 |  |
| serde | 1.0.229 | MIT OR Apache-2.0 | https://crates.io/crates/serde/1.0.229 |  |
| serde-untagged | 0.1.9 | MIT OR Apache-2.0 | https://crates.io/crates/serde-untagged/0.1.9 |  |
| serde_core | 1.0.229 | MIT OR Apache-2.0 | https://crates.io/crates/serde_core/1.0.229 |  |
| serde_derive | 1.0.229 | MIT OR Apache-2.0 | https://crates.io/crates/serde_derive/1.0.229 |  |
| serde_derive_internals | 0.29.1 | MIT OR Apache-2.0 | https://crates.io/crates/serde_derive_internals/0.29.1 |  |
| serde_json | 1.0.151 | MIT OR Apache-2.0 | https://crates.io/crates/serde_json/1.0.151 |  |
| serde_repr | 0.1.21 | MIT OR Apache-2.0 | https://crates.io/crates/serde_repr/0.1.21 |  |
| serde_spanned | 0.6.9 / 1.1.1 | MIT OR Apache-2.0 | https://crates.io/crates/serde_spanned/1.1.1 |  |
| serde_with | 3.23.0 | MIT OR Apache-2.0 | https://crates.io/crates/serde_with/3.23.0 |  |
| serde_with_macros | 3.23.0 | MIT OR Apache-2.0 | https://crates.io/crates/serde_with_macros/3.23.0 |  |
| serialize-to-javascript | 0.1.2 | MIT OR Apache-2.0 | https://crates.io/crates/serialize-to-javascript/0.1.2 |  |
| serialize-to-javascript-impl | 0.1.2 | MIT OR Apache-2.0 | https://crates.io/crates/serialize-to-javascript-impl/0.1.2 |  |
| servo_arc | 0.4.3 | MIT OR Apache-2.0 | https://crates.io/crates/servo_arc/0.4.3 |  |
| sha2 | 0.10.9 | MIT OR Apache-2.0 | https://crates.io/crates/sha2/0.10.9 |  |
| shlex | 2.0.1 | MIT OR Apache-2.0 | https://crates.io/crates/shlex/2.0.1 |  |
| signal-hook-registry | 1.4.8 | MIT OR Apache-2.0 | https://crates.io/crates/signal-hook-registry/1.4.8 |  |
| simd-adler32 | 0.3.10 | MIT | https://crates.io/crates/simd-adler32/0.3.10 |  |
| simd_cesu8 | 1.2.0 | UNKNOWN | https://crates.io/crates/simd_cesu8/1.2.0 | 来源待核：本地 registry Cargo.toml 未提供 license 字段（DEC-024 口径：UNKNOWN 保留，不阻断） |
| simdutf8 | 0.1.5 | MIT OR Apache-2.0 | https://crates.io/crates/simdutf8/0.1.5 |  |
| siphasher | 1.0.3 | MIT/Apache-2.0 | https://crates.io/crates/siphasher/1.0.3 |  |
| slab | 0.4.12 | MIT | https://crates.io/crates/slab/0.4.12 |  |
| smallvec | 1.16.1 | MIT OR Apache-2.0 | https://crates.io/crates/smallvec/1.16.1 |  |
| socket2 | 0.6.5 | MIT OR Apache-2.0 | https://crates.io/crates/socket2/0.6.5 |  |
| softbuffer | 0.4.8 | MIT OR Apache-2.0 | https://crates.io/crates/softbuffer/0.4.8 |  |
| soup3 | 0.5.0 | MIT | https://crates.io/crates/soup3/0.5.0 |  |
| soup3-sys | 0.5.0 | MIT | https://crates.io/crates/soup3-sys/0.5.0 |  |
| stable_deref_trait | 1.2.1 | MIT OR Apache-2.0 | https://crates.io/crates/stable_deref_trait/1.2.1 |  |
| string_cache | 0.9.0 | MIT OR Apache-2.0 | https://crates.io/crates/string_cache/0.9.0 |  |
| string_cache_codegen | 0.6.1 | MIT OR Apache-2.0 | https://crates.io/crates/string_cache_codegen/0.6.1 |  |
| strsim | 0.11.1 | MIT | https://crates.io/crates/strsim/0.11.1 |  |
| subtle | 2.6.1 | BSD-3-Clause | https://crates.io/crates/subtle/2.6.1 |  |
| swift-rs | 1.0.8 | MIT OR Apache-2.0 | https://crates.io/crates/swift-rs/1.0.8 |  |
| syn | 1.0.109 / 2.0.119 / 3.0.5 | MIT OR Apache-2.0 | https://crates.io/crates/syn/3.0.5 |  |
| sync_wrapper | 1.0.2 | Apache-2.0 | https://crates.io/crates/sync_wrapper/1.0.2 |  |
| synstructure | 0.13.2 | MIT | https://crates.io/crates/synstructure/0.13.2 |  |
| system-deps | 6.2.2 | MIT OR Apache-2.0 | https://crates.io/crates/system-deps/6.2.2 |  |
| tao | 0.35.3 | Apache-2.0 | https://crates.io/crates/tao/0.35.3 |  |
| tao-macros | 0.1.4 | MIT OR Apache-2.0 | https://crates.io/crates/tao-macros/0.1.4 |  |
| tar | 0.4.46 | MIT OR Apache-2.0 | https://crates.io/crates/tar/0.4.46 |  |
| target-lexicon | 0.12.16 | Apache-2.0 WITH LLVM-exception | https://crates.io/crates/target-lexicon/0.12.16 |  |
| tauri | 2.11.5 | Apache-2.0 OR MIT | https://crates.io/crates/tauri/2.11.5 |  |
| tauri-build | 2.6.3 | Apache-2.0 OR MIT | https://crates.io/crates/tauri-build/2.6.3 |  |
| tauri-codegen | 2.6.3 | Apache-2.0 OR MIT | https://crates.io/crates/tauri-codegen/2.6.3 |  |
| tauri-macros | 2.6.3 | Apache-2.0 OR MIT | https://crates.io/crates/tauri-macros/2.6.3 |  |
| tauri-plugin | 2.6.3 | Apache-2.0 OR MIT | https://crates.io/crates/tauri-plugin/2.6.3 |  |
| tauri-plugin-opener | 2.5.5 | Apache-2.0 OR MIT | https://crates.io/crates/tauri-plugin-opener/2.5.5 |  |
| tauri-plugin-process | 2.3.1 | Apache-2.0 OR MIT | https://crates.io/crates/tauri-plugin-process/2.3.1 |  |
| tauri-plugin-updater | 2.10.1 | Apache-2.0 OR MIT | https://crates.io/crates/tauri-plugin-updater/2.10.1 |  |
| tauri-runtime | 2.11.3 | Apache-2.0 OR MIT | https://crates.io/crates/tauri-runtime/2.11.3 |  |
| tauri-runtime-wry | 2.11.4 | Apache-2.0 OR MIT | https://crates.io/crates/tauri-runtime-wry/2.11.4 |  |
| tauri-utils | 2.9.3 | Apache-2.0 OR MIT | https://crates.io/crates/tauri-utils/2.9.3 |  |
| tauri-winres | 0.3.6 | MIT | https://crates.io/crates/tauri-winres/0.3.6 |  |
| tempfile | 3.27.0 | MIT OR Apache-2.0 | https://crates.io/crates/tempfile/3.27.0 |  |
| tendril | 0.5.1 | MIT OR Apache-2.0 | https://crates.io/crates/tendril/0.5.1 |  |
| thiserror | 1.0.69 / 2.0.20 | MIT OR Apache-2.0 | https://crates.io/crates/thiserror/2.0.20 |  |
| thiserror-impl | 1.0.69 / 2.0.20 | MIT OR Apache-2.0 | https://crates.io/crates/thiserror-impl/2.0.20 |  |
| time | 0.3.55 | MIT OR Apache-2.0 | https://crates.io/crates/time/0.3.55 |  |
| time-core | 0.1.9 | MIT OR Apache-2.0 | https://crates.io/crates/time-core/0.1.9 |  |
| time-macros | 0.2.32 | MIT OR Apache-2.0 | https://crates.io/crates/time-macros/0.2.32 |  |
| tinystr | 0.8.4 | Unicode-3.0 | https://crates.io/crates/tinystr/0.8.4 |  |
| tinyvec | 1.13.2 | Zlib OR Apache-2.0 OR MIT | https://crates.io/crates/tinyvec/1.13.2 |  |
| tinyvec_macros | 0.1.1 | MIT OR Apache-2.0 OR Zlib | https://crates.io/crates/tinyvec_macros/0.1.1 |  |
| tokio | 1.53.1 | MIT | https://crates.io/crates/tokio/1.53.1 |  |
| tokio-rustls | 0.26.4 | MIT OR Apache-2.0 | https://crates.io/crates/tokio-rustls/0.26.4 |  |
| tokio-util | 0.7.19 | MIT | https://crates.io/crates/tokio-util/0.7.19 |  |
| toml | 0.8.2 / 0.9.12+spec-1.1.0 / 1.1.6+spec-1.1.0 | MIT OR Apache-2.0 | https://crates.io/crates/toml/1.1.6+spec-1.1.0 |  |
| toml_datetime | 0.6.3 / 0.7.5+spec-1.1.0 / 1.1.1+spec-1.1.0 | MIT OR Apache-2.0 | https://crates.io/crates/toml_datetime/1.1.1+spec-1.1.0 |  |
| toml_edit | 0.19.15 / 0.20.2 / 0.25.15+spec-1.1.0 | MIT OR Apache-2.0 | https://crates.io/crates/toml_edit/0.25.15+spec-1.1.0 |  |
| toml_parser | 1.1.3+spec-1.1.0 | MIT OR Apache-2.0 | https://crates.io/crates/toml_parser/1.1.3+spec-1.1.0 |  |
| toml_writer | 1.1.2+spec-1.1.0 | MIT OR Apache-2.0 | https://crates.io/crates/toml_writer/1.1.2+spec-1.1.0 |  |
| tower | 0.5.3 | MIT | https://crates.io/crates/tower/0.5.3 |  |
| tower-http | 0.6.11 | MIT | https://crates.io/crates/tower-http/0.6.11 |  |
| tower-layer | 0.3.3 | MIT | https://crates.io/crates/tower-layer/0.3.3 |  |
| tower-service | 0.3.3 | MIT | https://crates.io/crates/tower-service/0.3.3 |  |
| tracing | 0.1.44 | MIT | https://crates.io/crates/tracing/0.1.44 |  |
| tracing-attributes | 0.1.31 | MIT | https://crates.io/crates/tracing-attributes/0.1.31 |  |
| tracing-core | 0.1.36 | MIT | https://crates.io/crates/tracing-core/0.1.36 |  |
| tray-icon | 0.24.2 | MIT OR Apache-2.0 | https://crates.io/crates/tray-icon/0.24.2 |  |
| try-lock | 0.2.5 | MIT | https://crates.io/crates/try-lock/0.2.5 |  |
| typeid | 1.0.3 | MIT OR Apache-2.0 | https://crates.io/crates/typeid/1.0.3 |  |
| typenum | 1.20.1 | MIT OR Apache-2.0 | https://crates.io/crates/typenum/1.20.1 |  |
| uds_windows | 1.2.1 | MIT | https://crates.io/crates/uds_windows/1.2.1 |  |
| unic-char-property | 0.9.0 | MIT/Apache-2.0 | https://crates.io/crates/unic-char-property/0.9.0 |  |
| unic-char-range | 0.9.0 | MIT/Apache-2.0 | https://crates.io/crates/unic-char-range/0.9.0 |  |
| unic-common | 0.9.0 | MIT/Apache-2.0 | https://crates.io/crates/unic-common/0.9.0 |  |
| unic-ucd-ident | 0.9.0 | MIT/Apache-2.0 | https://crates.io/crates/unic-ucd-ident/0.9.0 |  |
| unic-ucd-version | 0.9.0 | MIT/Apache-2.0 | https://crates.io/crates/unic-ucd-version/0.9.0 |  |
| unicode-ident | 1.0.24 | (MIT OR Apache-2.0) AND Unicode-3.0 | https://crates.io/crates/unicode-ident/1.0.24 |  |
| unicode-segmentation | 1.13.3 | MIT OR Apache-2.0 | https://crates.io/crates/unicode-segmentation/1.13.3 |  |
| untrusted | 0.9.0 | ISC | https://crates.io/crates/untrusted/0.9.0 |  |
| url | 2.5.8 | MIT OR Apache-2.0 | https://crates.io/crates/url/2.5.8 |  |
| urlpattern | 0.3.0 | MIT | https://crates.io/crates/urlpattern/0.3.0 |  |
| utf8_iter | 1.0.4 | Apache-2.0 OR MIT | https://crates.io/crates/utf8_iter/1.0.4 |  |
| uuid | 1.26.1 | Apache-2.0 OR MIT | https://crates.io/crates/uuid/1.26.1 |  |
| version-compare | 0.2.1 | MIT | https://crates.io/crates/version-compare/0.2.1 |  |
| version_check | 0.9.5 | MIT/Apache-2.0 | https://crates.io/crates/version_check/0.9.5 |  |
| vswhom | 0.1.0 | MIT | https://crates.io/crates/vswhom/0.1.0 |  |
| vswhom-sys | 0.1.3 | MIT | https://crates.io/crates/vswhom-sys/0.1.3 |  |
| walkdir | 2.5.0 | Unlicense/MIT | https://crates.io/crates/walkdir/2.5.0 |  |
| want | 0.3.1 | MIT | https://crates.io/crates/want/0.3.1 |  |
| wasi | 0.11.1+wasi-snapshot-preview1 | Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT | https://crates.io/crates/wasi/0.11.1+wasi-snapshot-preview1 |  |
| wasip2 | 1.0.4+wasi-0.2.12 | Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT | https://crates.io/crates/wasip2/1.0.4+wasi-0.2.12 |  |
| wasm-bindgen | 0.2.128 | MIT OR Apache-2.0 | https://crates.io/crates/wasm-bindgen/0.2.128 |  |
| wasm-bindgen-futures | 0.4.78 | MIT OR Apache-2.0 | https://crates.io/crates/wasm-bindgen-futures/0.4.78 |  |
| wasm-bindgen-macro | 0.2.128 | MIT OR Apache-2.0 | https://crates.io/crates/wasm-bindgen-macro/0.2.128 |  |
| wasm-bindgen-macro-support | 0.2.128 | MIT OR Apache-2.0 | https://crates.io/crates/wasm-bindgen-macro-support/0.2.128 |  |
| wasm-bindgen-shared | 0.2.128 | MIT OR Apache-2.0 | https://crates.io/crates/wasm-bindgen-shared/0.2.128 |  |
| wasm-streams | 0.5.0 | MIT OR Apache-2.0 | https://crates.io/crates/wasm-streams/0.5.0 |  |
| web-sys | 0.3.105 | MIT OR Apache-2.0 | https://crates.io/crates/web-sys/0.3.105 |  |
| web_atoms | 0.2.6 | MIT OR Apache-2.0 | https://crates.io/crates/web_atoms/0.2.6 |  |
| webkit2gtk | 2.0.2 | MIT | https://crates.io/crates/webkit2gtk/2.0.2 |  |
| webkit2gtk-sys | 2.0.2 | MIT | https://crates.io/crates/webkit2gtk-sys/2.0.2 |  |
| webpki-root-certs | 1.0.9 | UNKNOWN | https://crates.io/crates/webpki-root-certs/1.0.9 | 来源待核：本地 registry Cargo.toml 未提供 license 字段（DEC-024 口径：UNKNOWN 保留，不阻断） |
| webview2-com | 0.38.2 | MIT | https://crates.io/crates/webview2-com/0.38.2 |  |
| webview2-com-macros | 0.8.1 | MIT | https://crates.io/crates/webview2-com-macros/0.8.1 |  |
| webview2-com-sys | 0.38.2 | MIT | https://crates.io/crates/webview2-com-sys/0.38.2 |  |
| winapi | 0.3.9 | MIT/Apache-2.0 | https://crates.io/crates/winapi/0.3.9 |  |
| winapi-i686-pc-windows-gnu | 0.4.0 | MIT/Apache-2.0 | https://crates.io/crates/winapi-i686-pc-windows-gnu/0.4.0 |  |
| winapi-util | 0.1.11 | Unlicense OR MIT | https://crates.io/crates/winapi-util/0.1.11 |  |
| winapi-x86_64-pc-windows-gnu | 0.4.0 | MIT/Apache-2.0 | https://crates.io/crates/winapi-x86_64-pc-windows-gnu/0.4.0 |  |
| window-vibrancy | 0.6.0 | Apache-2.0 OR MIT | https://crates.io/crates/window-vibrancy/0.6.0 |  |
| windows | 0.61.3 | MIT OR Apache-2.0 | https://crates.io/crates/windows/0.61.3 |  |
| windows-collections | 0.2.0 | MIT OR Apache-2.0 | https://crates.io/crates/windows-collections/0.2.0 |  |
| windows-core | 0.61.2 / 0.62.2 | MIT OR Apache-2.0 | https://crates.io/crates/windows-core/0.62.2 |  |
| windows-future | 0.2.1 | MIT OR Apache-2.0 | https://crates.io/crates/windows-future/0.2.1 |  |
| windows-implement | 0.60.2 | MIT OR Apache-2.0 | https://crates.io/crates/windows-implement/0.60.2 |  |
| windows-interface | 0.59.3 | MIT OR Apache-2.0 | https://crates.io/crates/windows-interface/0.59.3 |  |
| windows-link | 0.1.3 / 0.2.1 | MIT OR Apache-2.0 | https://crates.io/crates/windows-link/0.2.1 |  |
| windows-numerics | 0.2.0 | MIT OR Apache-2.0 | https://crates.io/crates/windows-numerics/0.2.0 |  |
| windows-result | 0.3.4 / 0.4.1 | MIT OR Apache-2.0 | https://crates.io/crates/windows-result/0.4.1 |  |
| windows-strings | 0.4.2 / 0.5.1 | MIT OR Apache-2.0 | https://crates.io/crates/windows-strings/0.5.1 |  |
| windows-sys | 0.45.0 / 0.52.0 / 0.59.0 / 0.60.2 / 0.61.2 | MIT OR Apache-2.0 / UNKNOWN / MIT OR Apache-2.0 / UNKNOWN / MIT OR Apache-2.0 | https://crates.io/crates/windows-sys/0.61.2 | 来源待核：本地 registry Cargo.toml 未提供 license 字段（DEC-024 口径：UNKNOWN 保留，不阻断） |
| windows-targets | 0.42.2 / 0.52.6 / 0.53.5 | MIT OR Apache-2.0 / MIT OR Apache-2.0 / UNKNOWN | https://crates.io/crates/windows-targets/0.53.5 | 来源待核：本地 registry Cargo.toml 未提供 license 字段（DEC-024 口径：UNKNOWN 保留，不阻断） |
| windows-threading | 0.1.0 | MIT OR Apache-2.0 | https://crates.io/crates/windows-threading/0.1.0 |  |
| windows-version | 0.1.7 | MIT OR Apache-2.0 | https://crates.io/crates/windows-version/0.1.7 |  |
| windows_aarch64_gnullvm | 0.42.2 / 0.52.6 / 0.53.1 | MIT OR Apache-2.0 / MIT OR Apache-2.0 / UNKNOWN | https://crates.io/crates/windows_aarch64_gnullvm/0.53.1 | 来源待核：本地 registry Cargo.toml 未提供 license 字段（DEC-024 口径：UNKNOWN 保留，不阻断） |
| windows_aarch64_msvc | 0.42.2 / 0.52.6 / 0.53.1 | MIT OR Apache-2.0 / MIT OR Apache-2.0 / UNKNOWN | https://crates.io/crates/windows_aarch64_msvc/0.53.1 | 来源待核：本地 registry Cargo.toml 未提供 license 字段（DEC-024 口径：UNKNOWN 保留，不阻断） |
| windows_i686_gnu | 0.42.2 / 0.52.6 / 0.53.1 | MIT OR Apache-2.0 / MIT OR Apache-2.0 / UNKNOWN | https://crates.io/crates/windows_i686_gnu/0.53.1 | 来源待核：本地 registry Cargo.toml 未提供 license 字段（DEC-024 口径：UNKNOWN 保留，不阻断） |
| windows_i686_gnullvm | 0.52.6 / 0.53.1 | MIT OR Apache-2.0 / UNKNOWN | https://crates.io/crates/windows_i686_gnullvm/0.53.1 | 来源待核：本地 registry Cargo.toml 未提供 license 字段（DEC-024 口径：UNKNOWN 保留，不阻断） |
| windows_i686_msvc | 0.42.2 / 0.52.6 / 0.53.1 | MIT OR Apache-2.0 / MIT OR Apache-2.0 / UNKNOWN | https://crates.io/crates/windows_i686_msvc/0.53.1 | 来源待核：本地 registry Cargo.toml 未提供 license 字段（DEC-024 口径：UNKNOWN 保留，不阻断） |
| windows_x86_64_gnu | 0.42.2 / 0.52.6 / 0.53.1 | MIT OR Apache-2.0 / MIT OR Apache-2.0 / UNKNOWN | https://crates.io/crates/windows_x86_64_gnu/0.53.1 | 来源待核：本地 registry Cargo.toml 未提供 license 字段（DEC-024 口径：UNKNOWN 保留，不阻断） |
| windows_x86_64_gnullvm | 0.42.2 / 0.52.6 / 0.53.1 | MIT OR Apache-2.0 / MIT OR Apache-2.0 / UNKNOWN | https://crates.io/crates/windows_x86_64_gnullvm/0.53.1 | 来源待核：本地 registry Cargo.toml 未提供 license 字段（DEC-024 口径：UNKNOWN 保留，不阻断） |
| windows_x86_64_msvc | 0.42.2 / 0.52.6 / 0.53.1 | MIT OR Apache-2.0 / MIT OR Apache-2.0 / UNKNOWN | https://crates.io/crates/windows_x86_64_msvc/0.53.1 | 来源待核：本地 registry Cargo.toml 未提供 license 字段（DEC-024 口径：UNKNOWN 保留，不阻断） |
| winnow | 0.5.40 / 0.7.15 / 1.0.4 | MIT | https://crates.io/crates/winnow/1.0.4 |  |
| winreg | 0.55.0 | MIT | https://crates.io/crates/winreg/0.55.0 |  |
| wit-bindgen | 0.57.1 | Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT | https://crates.io/crates/wit-bindgen/0.57.1 |  |
| writeable | 0.6.4 | Unicode-3.0 | https://crates.io/crates/writeable/0.6.4 |  |
| wry | 0.55.1 | Apache-2.0 OR MIT | https://crates.io/crates/wry/0.55.1 |  |
| x11 | 2.21.0 | MIT | https://crates.io/crates/x11/2.21.0 |  |
| x11-dl | 2.21.0 | MIT | https://crates.io/crates/x11-dl/2.21.0 |  |
| xattr | 1.6.1 | MIT OR Apache-2.0 | https://crates.io/crates/xattr/1.6.1 |  |
| yoke | 0.8.3 | Unicode-3.0 | https://crates.io/crates/yoke/0.8.3 |  |
| yoke-derive | 0.8.2 | Unicode-3.0 | https://crates.io/crates/yoke-derive/0.8.2 |  |
| zbus | 5.19.0 | MIT | https://crates.io/crates/zbus/5.19.0 |  |
| zbus_macros | 5.19.0 | MIT | https://crates.io/crates/zbus_macros/5.19.0 |  |
| zbus_names | 4.3.4 | MIT | https://crates.io/crates/zbus_names/4.3.4 |  |
| zcheapstr | 1.1.0 | MIT | https://crates.io/crates/zcheapstr/1.1.0 |  |
| zerofrom | 0.1.8 | Unicode-3.0 | https://crates.io/crates/zerofrom/0.1.8 |  |
| zerofrom-derive | 0.1.7 | Unicode-3.0 | https://crates.io/crates/zerofrom-derive/0.1.7 |  |
| zeroize | 1.9.0 | Apache-2.0 OR MIT | https://crates.io/crates/zeroize/1.9.0 |  |
| zerotrie | 0.2.5 | Unicode-3.0 | https://crates.io/crates/zerotrie/0.2.5 |  |
| zerovec | 0.11.8 | Unicode-3.0 | https://crates.io/crates/zerovec/0.11.8 |  |
| zerovec-derive | 0.11.6 | Unicode-3.0 | https://crates.io/crates/zerovec-derive/0.11.6 |  |
| zip | 4.6.1 | UNKNOWN | https://crates.io/crates/zip/4.6.1 | 来源待核：本地 registry Cargo.toml 未提供 license 字段（DEC-024 口径：UNKNOWN 保留，不阻断） |
| zlib-rs | 0.6.7 | Zlib | https://crates.io/crates/zlib-rs/0.6.7 |  |
| zmij | 1.0.23 | MIT | https://crates.io/crates/zmij/1.0.23 |  |
| zvariant | 5.15.0 | MIT | https://crates.io/crates/zvariant/5.15.0 |  |
| zvariant_derive | 5.15.0 | MIT | https://crates.io/crates/zvariant_derive/5.15.0 |  |
| zvariant_utils | 4.2.0 | MIT | https://crates.io/crates/zvariant_utils/4.2.0 |  |

## 4. 前端 vendored 资源

| 资源 | 来源 | 许可证 | 发行影响 |
| --- | --- | --- | --- |
| `frontend/vendor/echarts.min.js`（文件内核心 export 标识 5.6.0） | https://echarts.apache.org | Apache-2.0（ASF 声明随文件头分发，原文如下） | 随前端 bundle 公开分发，许可证全文随产物附带即可覆盖 |
| 内嵌 tslib（随 ECharts bundle；npm 元数据 tslib 2.3.0） | Microsoft tslib（随 echarts 官方 bundle 组合分发） | 0BSD（Microsoft 版权段随文件头分发，原文如下） | 0BSD 不要求附文；但保留原文以保持来源可追溯 |
| 内嵌 zrender（随 ECharts bundle） | https://github.com/ecomfe/zrender | UNKNOWN（随 echarts 官方 dist 组合分发，未单独核对；NOT_VERIFIED） | 不构成独立发行组件，仅作为 ECharts 内部模块随 bundle 一并分发；正式发行前需补 zrender 来源 URL + SHA-256 + 许可文本 |

ASF Apache-2.0 声明原文（`frontend/vendor/echarts.min.js` 文件头随分发未删改）：

> Licensed to the Apache Software Foundation (ASF) under one
> or more contributor license agreements. See the NOTICE file
> distributed with this work for additional information
> regarding copyright ownership. The ASF licenses this file
> to you under the Apache License, Version 2.0 (the
> "License"); you may not use this file except in compliance
> with the License. You may obtain a copy of the License at
>
>   http://www.apache.org/licenses/LICENSE-2.0

Microsoft tslib 0BSD 声明原文（随 ECharts bundle 文件头）：

> Copyright (c) Microsoft Corporation.
>
> Permission to use, copy, modify, and/or distribute this software for any
> purpose with or without fee is hereby granted.

完整 Apache-2.0 文本：https://www.apache.org/licenses/LICENSE-2.0.txt。

## 5. 图形资源

> 说明：以下两类资产权利来源不同——深潭 App 图标的**原稿**来自 image_gen
> 生成（工具生成物权利限制见 DEC-023 与 assets/brand/README.md），处理链
> 由 `scripts/build_app_icon.py` 本地 Pillow 完成（无第三方代码）；界面用
> 深度环 SVG 是**自绘几何**（自有权利）。两者分别登记，不扩大权利保证。

| 资源 | 来源 | 许可证 | 发行影响 |
| --- | --- | --- | --- |
| 深潭 App 图标 `apps/desktop/src-tauri/icons/icon.{png,icns}`（824/1024 底板 + 1024×1024 PNG；macOS 11+ 图标网格） | `assets/brand/README.md`（DEC-023）：内置 image_gen 原稿 `fathom-approved-concept.png` 经 `scripts/build_app_icon.py` 本地 Pillow 处理（容差 28 BFS flood fill 抠图 → bbox 方形裁剪 → 1024 缩放 → 边缘 α<36 归零） | 自有（项目原创；工具生成物权利限制见 DEC-023，不作超出已知事实的保证） | 作为发行资产无第三方义务；不复制 Folia/FaroPDF/Funes 主体 |
| 深度环 SVG（界面小尺寸场景：菜单栏 tray、侧栏字标、favicon） | `frontend/icons.js` | 自有（自绘几何，无第三方权利） | 小尺寸场景保留——四层深潭在灰度下不可辨，属 DEC-023 明确取舍 |
| `apps/desktop/src-tauri/icons/tray.png`（菜单栏 22pt） | `scripts/make_tray_icon.py`（纯 stdlib 生成） | 自有（项目原创，无第三方权利） | 随 bundle 分发 |

> 历史注记：PR #116 曾短暂以线条深度环作为 App 图标（2026-09-19 凌晨合并），
> 当日由深潭形态替换（DEC-023）；tray/字标/favicon 交付继续有效。
>
> 2026-09-14 版 notices 曾将 `icon.png` 误记为「Tauri 脚手架占位／UNKNOWN／挂 ISS-045」。
> 该描述与 main 现实不符——`icon.png` 已由 PR #117（commit 55d6c3f，2026-09-19）
> 替换为 DEC-023 深潭 App 图标（`assets/brand/fathom-approved-concept.png` 经
> `scripts/build_app_icon.py` 本地 Pillow 处理），随后 e033ef6 把底板缩至
> 824/1024（80.5%）。`icon.{png,icns}` 现已在首行与同一深潭链统一登记，
> 此处不再单列；ISS-045 用户门替换闭环自动作废。
