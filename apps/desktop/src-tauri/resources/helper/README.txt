# ISS-009 切片 1 · 内嵌 helper 占位目录

打包时由 `scripts/build_helper.sh` 把 PyInstaller onedir 冻结产物放到这里：

    resources/helper/fathom-helper/fathom-helper

tauri.conf.json 的 bundle.resources 指向 `resources/helper/**/*`（ISS-053
修复：原 `**` 在 Tauri 2.x 的 glob 实现里只匹配路径段、不匹配叶文件，会
触发 `glob pattern resources/helper/** path not found or didn't match any files.`
硬错误；`**/*` 才是「零或多级目录 + 一个文件」的常用写法）。Tauri 在
打包时把整个目录拷到 `.app/Contents/Resources/helper/`，同时把 README.txt
作为占位文件保留——这样在未跑 build_helper.sh 的干净克隆上，build script
也能匹配到至少一个文件，不会硬错。

两种状态都成立：
- 干净克隆（本 README 唯一文件）：`**/*` 匹配 `README.txt`，build 通过
- 已跑 build_helper.sh：`**/*` 匹配 `fathom-helper/fathom-helper` 与
  `_internal/*`，build 通过

开发态不依赖冻结产物：设置环境变量

    FATHOM_HELPER_BIN=/path/to/fathom-helper

由 Rust 壳 `apps/desktop/src-tauri/src/helper.rs::locate_helper` 读取。

本目录已通过仓库根 .gitignore 忽略冻结产物本身；本 README 与后续资源
manifest（NOT_VERIFIED 占位）保留入库。
