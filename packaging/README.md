# Fathom v0.3 打包布局提案（ISS-029 产出，目标方案）

状态：提案（PROPOSAL）——未构建、未签名、未公证；实施归 ISS-009/041
依据：[发行方案](../docs/plans/2026-09-13-v0.3-release-design.md)与
[ISS-029 实验发现](../apps/desktop/experiments/iss029/findings.md)

## .app 布局（arm64 与 x86_64 各自原生构建，不做 universal2）

```
Fathom.app/Contents/
  MacOS/Fathom                  # Tauri 壳（发行方案：由 Rust 壳协调更新）
  Resources/
    helper/                     # PyInstaller onedir 产物（只读，嵌套签名）
      fathom-helper             #   冻结入口（含 --version 身份面，G3 改造后）
      _internal/                #   fathom 包 + fastapi/uvicorn + Python stdlib
  Frameworks/                   # Tauri 依赖
  Info.plist
```

- **资源只读不变量**：bundle 内任何路径不得出现运行期写入；helper 的
  `data/`、`reports/`、`logs/`、DB 一律落 `FATHOM_DATA_ROOT`（提案值
  `~/Library/Application Support/Fathom/`）——生产 config 需按 G2 改造。
- 升级前 helper 停写并一致备份 SQLite（发行方案 §4），停写握手用
  discovery 文件 + token（iss029 原型语义）。

## 签名与公证顺序（ISS-041 执行）

1. 最内层先签：`Resources/helper/fathom-helper` 与 `_internal` 内
   Mach-O/so 逐层 → `Resources/helper` 整体 → Frameworks → 最外层 app
   （`--deep` 不作为唯一手段，按嵌套顺序显式签）。
2. hardened runtime；helper 无 JIT 需求，预计无需例外 entitlements
   （以实际 `codesign --verify --deep --strict` + 公证结果为准）。
3. `spctl --assess --type execute`、`xcrun stapler validate` 过门槛。
4. updater 产物与 minisign 密钥链路见发行方案 §5（独立于本提案）。

## 双架构复验（ISS-041 清单摘要）

- x86_64 原生 runner、同 pins、python.org 解释器（setup-python）。
- `file`（仅 x86_64）、`otool -L`、`lipo -info`、干净账户断网首启。
- 详细步骤见 findings.md §8；本目录不承载任务状态。
