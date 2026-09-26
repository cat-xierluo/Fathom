# Fathom 开源许可证选项对比（供用户选择）

- 日期：2026-09-14（ISS-037）。**本文件只做对比与建议，不创建 LICENSE；
  LICENSE 仅在用户明确选择后落地（人工门）。**仓库转公开是另一个独立
  人工门，与本选择解耦。
- 对象：Fathom v0.3.0——macOS 桌面应用（Tauri/Rust 壳）＋ 内嵌冻结
  Python helper（CPython 3.14.6 + FastAPI/uvicorn）＋ vendored ECharts
  前端资源，自研核心代码（scanner/db/reports/api/cli/frontend）。
- 依赖许可事实来源：[依赖与来源清单](2026-09-14-dependency-inventory.md)
  （`2026-09-14-dependency-inventory.md`）与 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。

## 1. 候选概览

| 维度 | MIT | Apache-2.0 | MPL-2.0 | AGPL-3.0 |
|---|---|---|---|---|
| 类型 | 宽松 | 宽松＋显式专利授权 | 文件级弱 copyleft | 强 copyleft（含网络使用） |
| 典型义务 | 保留版权与许可声明 | 同左＋NOTICE 机制 | 修改过的 MPL 文件须以 MPL 开源；可与其他许可代码组合 | 分发二进制须提供对应源码（含构建脚本）；网络交互亦可触发源码义务 |
| 对闭源 fork | 允许 | 允许 | 允许（未改 MPL 文件的部分） | 禁止（除非单独购买双许可） |
| 商用/双许可灵活性 | 最高 | 最高 | 高（整体可商用，改动文件须回传） | 低（商用集成方通常回避） |
| 贡献者摩擦 | 最低 | 低 | 中 | 高（贡献者代码将强制同许可传播） |
| 合规负担 | 最低 | 低 | 中（文件头/声明维护） | 最高（每次分发附源码要约；版本发布流程需固化） |
| 文本长度/复杂度 | 最短 | 中 | 中 | 长 |

## 2. 与现有依赖的兼容矩阵

依赖侧全部为宽松许可（无一 copyleft）：

| 依赖（许可证） | MIT | Apache-2.0 | MPL-2.0 | AGPL-3.0 |
|---|---|---|---|---|
| fastapi / pydantic / tokio / objc2 / ico / h11（MIT） | ✓ | ✓ | ✓ | ✓（作为 AGPL 作品的部分保留原许可） |
| tauri / wry / tauri-build / tauri-plugin-opener（Apache-2.0 OR MIT） | ✓ | ✓ | ✓ | ✓ |
| **tao（仅 Apache-2.0）** | ✓（依赖自身保持 Apache-2.0，义务为保留声明） | ✓ | ✓ | ✓（Apache-2.0 与 GPLv3/AGPLv3 兼容） |
| uvicorn / starlette / click（BSD-3-Clause） | ✓ | ✓ | ✓ | ✓ |
| serde / serde_json / log / libc / getrandom / semver / http / url / png / thiserror（MIT OR Apache-2.0） | ✓ | ✓ | ✓ | ✓ |
| CPython 3.14.6（PSF-2.0） | ✓ | ✓ | ✓ | ✓（Python 官方声明 GPL 兼容） |
| ECharts vendored（Apache-2.0，含内嵌 tslib 0BSD） | ✓（ECharts 保持 Apache-2.0；§4.1 保留声明/NOTICE 义务已在 THIRD_PARTY_NOTICES 处理） | ✓ | ✓ | ✓ |
| PyInstaller bootloader（GPLv2-or-later ＋ 例外：允许构建分发非自由/商用程序） | ✓（依赖例外条款） | ✓（同左） | ✓（同左） | ✓ |
| typing-extensions 等未核对项（UNKNOWN） | 待核对 | 待核对 | 待核对 | 待核对 |

结论：**四个候选与已核对依赖均兼容**；tao 的单 Apache-2.0 与 PyInstaller
的 GPL 例外是仅有的两处需要留存书面依据的点（本方案与依赖清单已记录
证据）。UNKNOWN 项在 LICENSE 落地前应完成核对（宽松许可生态，预期无
阻塞，但以核对为准）。

## 3. 对本项目形态的具体影响

**MIT**
- 最省事：一份短文本＋版权行；对 ISS-041 打包只需随产物带 LICENSE 与
  THIRD_PARTY_NOTICES。
- 无显式专利授权；被闭源 fork 或商用再分发无任何约束（看个人意图，
  这可以是优点也可以是缺点）。
- 与 Tauri 生态主流选择一致（多数 crate 是 MIT OR Apache-2.0 双许可）。

**Apache-2.0**
- 相比 MIT 多：显式专利授权与反诉终止条款、商标明确不授权、NOTICE
  机制——对含 WebView/Rust 绑定的桌面应用是务实加分项。
- 义务多一项：分发时保留 NOTICE 类信息（我们的 notices 文件已按此
  结构准备）。
- 可与 MIT 双许可（`Apache-2.0 OR MIT`），与依赖生态完全同构。

**MPL-2.0**
- 文件级 copyleft：我们自己的源文件若标 MPL，任何人修改这些文件后
  分发必须开源该文件的新版本；但允许整体与专有代码组合（§3.3）。
- 效果：防止"改核心算法闭源卖"，又不吓退商用集成。
- 成本：需要给每个源文件维护许可证头（本项目目前无文件头惯例，
  引入是一次性机械工作＋长期纪律）；对 vendored ECharts/冻结 helper
  无影响。

**AGPL-3.0**
- 最强保护：任何分发（.app/.dmg）都必须提供对应完整源码与构建脚本；
  若未来出现"远程分析/托管服务"形态（路线图 M4 的可选 Agent 解释），
  网络交互也触发源码义务。
- 现实成本：① 每次 Release 必须同步公开源码（与"私有仓库＋公开
  Release"的当前策略冲突——AGPL 下不能只发二进制）；② 内嵌的每个
  第三方组件都要保留各自许可与源码可得的说明；③ 商用采用者与部分
  企业贡献者会直接回避；④ 若未来想做双许可商业化，需要 CLA 收集
  贡献者授权，否则无法切换。
- 对"本地优先、无账号、无云依赖"的 Fathom 当前形态，AGPL 的网络
  条款基本不会触发，但也几乎没有额外收益，除非把"防止闭源分叉"
  列为高优先级目标。

## 4. 推荐

**推荐 Apache-2.0（或 `Apache-2.0 OR MIT` 双许可）**：

1. 与全部已核对依赖零摩擦，且和 Tauri/Rust 生态默认形态一致；
2. 显式专利条款对绑定系统框架（WebKit/objc2）的桌面应用有实际保护；
3. 义务（保留声明＋NOTICE）已被本任务交付的 notices 结构覆盖，
  ISS-041 打包直接消费；
4. 为未来商业化/闭源衍生保留最大灵活性。

次选 MIT（更轻）；若明确希望"改动必须回流、但仍可商用组合"选
MPL-2.0；只有当"任何场景都不得闭源分叉"是硬目标时才选 AGPL-3.0
（并接受 Release 与源码公开绑定的流程约束）。

**再次强调：本推荐不构成决定。LICENSE 文件在用户明确选择前保持
不创建（当前仓库无 LICENSE 是预期状态，不是缺口）。**

## 5. 选择后的落地清单（届时执行，本任务不做）

1. 新建仓库根 LICENSE（所选许可证全文规范文本）；
2. README 增加许可证徽章/段落（PM 回写）；
3. THIRD_PARTY_NOTICES.md 联动（引用项目自身许可与 ECharts 等
   Apache-2.0 组件的 NOTICE 义务）；
4. ISS-041 打包把 LICENSE + THIRD_PARTY_NOTICES.md 纳入 .app/.dmg
   Resources；
5. 如选 MPL-2.0：为自有源文件补许可证头（新任务卡）；如选双许可：
   确定依优先顺序书写。

## 6. 选择结果（2026-09-14）

用户决定：**与 Folia 一致，采用 Apache-2.0**（Folia 仓库根 LICENSE 为 Apache License 2.0，`license = "Apache-2.0"`，版权行 `Copyright 2025-2026 maoking`）。落地：仓库根 LICENSE 复制 Folia 正文（逐字一致），版权行按 Fathom 最早提交年份写 `Copyright 2026 maoking`；`apps/desktop/src-tauri/Cargo.toml` 增加 `license = "Apache-2.0"`；README 与 THIRD_PARTY_NOTICES 联动；DEC-020 记录。第 5 节清单第 1–3 项已执行，第 4 项归 ISS-041 打包，第 5 项不适用（未选 MPL/双许可）。
