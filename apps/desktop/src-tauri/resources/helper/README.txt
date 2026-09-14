# ISS-009 切片 1 · 内嵌 helper 占位目录

打包时由 `scripts/build_helper.sh` 把 PyInstaller onedir 冻结产物放到这里：

    resources/helper/fathom-helper/fathom-helper

tauri.conf.json 的 bundle.resources 指向 `resources/helper/**`，Tauri 在
打包时把整个目录拷到 `.app/Contents/Resources/helper/`。

开发态不依赖冻结产物：设置环境变量

    FATHOM_HELPER_BIN=/path/to/fathom-helper

由 Rust 壳 `apps/desktop/src-tauri/src/helper.rs::locate_helper` 读取。

本目录已通过仓库根 .gitignore 忽略冻结产物本身；本 README 与后续资源
manifest（NOT_VERIFIED 占位）保留入库。
