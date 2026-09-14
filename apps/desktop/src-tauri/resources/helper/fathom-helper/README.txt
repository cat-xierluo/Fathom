# ISS-009 切片 1 · helper 冻结产物目录占位（ISS-055）

本目录是 tauri.conf.json `bundle.resources` map 映射的源目录：

    {"resources/helper/fathom-helper/": "helper/"}

PyInstaller onedir 冻结产物（`fathom-helper/` 子目录 + `fathom-helper.sha256`）
由 `scripts/build_helper.sh` 写到本目录内，打包后落在
`.app/Contents/Resources/helper/fathom-helper/fathom-helper`，与 Rust 壳
`helper.rs::locate_helper` 期望一致。

本 README.txt 是「源目录始终存在」的占位保证：map 的目录键在源目录缺失时
会触发 `resource path ... not found` 硬错误（cargo check 也会执行这段），
本文件保证干净克隆上目录可枚举，保住 ISS-053 的「无 helper 产物时
cargo check 仍 exit 0」约束。冻结产物本身由仓库根 .gitignore 忽略，
绝不入库；本 README 通过取反规则保留入库。
