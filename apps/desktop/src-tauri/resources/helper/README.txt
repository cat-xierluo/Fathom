# ISS-009 切片 1 · 内嵌 helper 资源目录

打包时由 `scripts/build_helper.sh` 把 PyInstaller onedir 冻结产物放到子目录
`fathom-helper/`（构建产物，gitignore 忽略，另有 `fathom-helper/README.txt`
占位说明）：

    resources/helper/fathom-helper/fathom-helper/fathom-helper   ← Mach-O 可执行

tauri.conf.json 的 bundle.resources 使用 **map 形式**
`{"resources/helper/fathom-helper/": "helper/"}`（ISS-055 修复）。Tauri 2.x
的 map 目录键不保留源前缀、保留去掉前缀后的相对结构，因此冻结树落到
`.app/Contents/Resources/helper/fathom-helper/...`，与 Rust 壳
`helper.rs::locate_helper` 期望的
`Contents/Resources/helper/fathom-helper/fathom-helper`（可执行文件本体）
对齐。

注意 map 键必须选在 `fathom-helper/`（PyInstaller `--distpath` 输出目录）：
更浅的 `resources/helper/` 会让产物多一层 `fathom-helper/` 目录、可执行文件
深一层，壳仍找不到；map 的 **glob 键**（含 `*`）会打平目录结构、破坏
onedir 的 `_internal/` 布局，同样不可用。历史上的数组 glob 形式
（ISS-053 的 `resources/helper/**/*`）则保留 `resources/` 前缀导致整体
错位一层，且空匹配会硬错误——三者都已被本 map 形式替代。

本 README 与 `fathom-helper/README.txt` 占位共同保证两种状态都成立：

- 干净克隆：map 源目录 `fathom-helper/` 因占位 README 存在且可枚举，
  build script（cargo check 也会跑到）不会触发 `resource path ... not
  found` 硬错误——保住 ISS-053 的「无 helper 产物时 cargo check 仍
  exit 0」约束
- 已跑 build_helper.sh：冻结树映射到 `helper/fathom-helper/...`，build 通过

开发态不依赖冻结产物：设置环境变量

    FATHOM_HELPER_BIN=/path/to/fathom-helper

由 Rust 壳 `apps/desktop/src-tauri/src/helper.rs::locate_helper` 读取。
