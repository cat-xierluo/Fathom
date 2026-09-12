# 交付架构与智能解释目标方案

日期：2026-09-12。**本文描述待实施目标，不是当前架构。** 用户已确认面向其他 Mac 用户、未来开源、加强 UX/UI、远期加入依赖识别与 Agent 解读；具体运行时选型仍需 ISS-029 实验。任务状态见 [TASKS](../TASKS.md)。

## 一、先保留内核，再补分发边界

| 路径 | 收益 | 代价 | 本次结论 |
|---|---|---|---|
| 自包含 Python helper + Tauri UI + macOS 管理后台服务 | 复用现有内核，用户不用装开发依赖；退出 UI 后仍能定时扫描 | 需验证打包、签名、服务注册、TCC、升级 | **推荐先实验** |
| 仅随 Tauri 启动 Python sidecar | 安装和启动路径较短 | 退出应用后无法沿用“无人值守扫描”承诺；另补调度会产生双重生命周期 | 仅作为受限内测备选，必须明确退出后行为 |
| 将内核重写为 Rust/Swift | 可减少运行时种类 | 重做扫描/SQLite/差分及大量验证，不能证明会更快 | 当前不做；实测失败再评估 |

Tauri 支持嵌入外部可执行文件，官方列举了打包 Python CLI/API 服务的用法；这只能证明打包路径存在，不能证明本项目后台服务和权限已经可用。[Tauri sidecar 文档](https://v2.tauri.app/develop/sidecar/)

macOS 13+ 的 SMAppService 可管理包内 helper 的 LoginItem/LaunchAgent，注册也受用户批准状态影响。因此实验先以 macOS 13+、Apple Silicon 为候选矩阵；这是建议的首批验证范围，不是已承诺的兼容范围。[Apple SMAppService](https://developer.apple.com/documentation/ServiceManagement/SMAppService?changes=la&language=objc)、[register](https://developer.apple.com/documentation/servicemanagement/smappservice/register%28%29)

拟议责任划分：

```text
Fathom.app
  ├─ Tauri：窗口、tray、首次启动、服务状态/注册入口
  ├─ 静态前端：展示事实、请求动作、可选解释
  └─ 自包含 helper：API + 统一扫描协调器 + 本地规则
         ↑ 单一后台服务所有者（由 ISS-029 确定）
         ├─ 系统调度/用户动作 → 同一协调器 → 有效快照
         └─ 用户数据目录 → SQLite / 报告 / 日志 / 配置
```

- “随包带 helper”与“由壳直接管理所有后台进程”是两件事。只允许一个组件负责后台服务启动、停止、重启；禁止 launchd 与 sidecar 同时争抢端口和扫描。
- 业务模块只接收明确的配置/连接，不在 import 阶段安装服务或访问真实 HOME；应用资源目录只读，运行时数据写用户 Application Support，日志写用户日志目录。
- 升级先停写并备份一致数据库，迁移成功后切换；版本不兼容时提示恢复备份，不让旧应用静默读写新 schema。普通卸载停服务，默认保留历史数据。
- 端口被占用时校验服务身份和协议版本；不能“HTTP 200 就跳转”。具体固定端口/动态发现与鉴权方案在 ISS-029 验证，任何故障都不得杀死未知占用进程。
- 保留开发用 CLI/浏览器入口；发行模式通过受控连接使用本地 API。WebView 对远端页面的 IPC 授权必须最小化并实测。[Tauri capabilities](https://v2.tauri.app/security/capabilities/)

直接下载的公开发行以签名、公证和真实下载后的启动验证为门槛。没有凭据时可推进本地构建与内测，但不能把它记为公开发行完成。[Tauri macOS signing](https://tauri.app/distribute/sign/macos/)、[Apple distribution signing](https://developer.apple.com/documentation/xcode/creating-distribution-signed-code-for-the-mac/)

## 二、扫描事实与业务状态

拟新增的扫描协调服务由 ISS-020 建立，API、CLI、定时任务全部调用它；避免三份扫描逻辑各自决定成功。

| 对象 | 必须保存/表达 | 不得推断 |
|---|---|---|
| 扫描运行 | run_id、来源、开始/结束、owner/存活信息、阶段、失败原因、有效 snapshot_id | 进程内锁空闲不代表其他进程没有扫描 |
| 有效快照 | 根/数据集身份、时间与时区、测量口径/阈值版本、覆盖质量、有效 entries、卷统计 | 空输出不等于空盘 |
| 对比结果 | a/b 精确 ID、同根同口径可比性、每项缺失原因/不确定性 | 低于阈值/权限变化不等于新建或删除 |
| 报告 | 基于哪两个快照、生成状态/时间、告警与口径 | 不能忽略 sid 参数然后取全局“最新两条” |

运行状态建议：`running → succeeded / partial / failed / interrupted`。`partial` 仅指产生了可解释的有效结果且有明确覆盖缺口；无根记录、严重解析失败或进程致命错误必须 failed。首扫成功但无对比基线，属于 `succeeded + report_not_available`；日报/通知失败单独记录，不回滚成功快照。对超时、取消和进程中断的子进程必须回收后才能释放租约/互斥。

同日覆盖在近期保留，但仅有效扫描成功后才替换同根当日数据；更换监控根或阈值形成新数据集，不混进旧趋势。缺失的历史点显示 gap，不补零、不平滑连接。保留期先如实标注现有“从今天向前 12 周”，后续策略版本化。

## 三、前端可维护性

保留原生前端，不为规划引入 React/Vite。ISS-027 按职责拆出请求/错误处理、状态与刷新、格式化、图表封装、各页面和 Tauri 桥；拟新增文件名由任务实现时在 `frontend/` 内确定。

全局只维护服务连接、扫描状态、当前有效快照列表与设置；页面维护筛选/选中目录。每个异步请求带世代号或可取消句柄，较旧响应不能覆盖新选择；轮询由一个所有者管理，离页后停止无关重查询。图表在可见容器初始化/resize，退场时有明确清理策略。

稳定响应由 FastAPI 模型与回归样例约束，前端错误区分：数据不足、无结果、正在执行、部分结果、服务失败、版本不兼容。不要把每种 500 都写成“明天再来”。详细交互见 [DESIGN](../DESIGN.md)。

## 四、智能识别先从可核查证据开始

用户场景：AI 工具为不同项目装了 Python venv、Node 依赖、共享包缓存、模型缓存等。仅按文件夹名字或体积无法知道用途，更无法证明可以删除。近期不为此创建全盘文件索引。

拟采用按需、小范围的本地识别器：

1. 用户选中的目录或已入库的大目录作为输入；按允许的深度/文件数/时间预算检查少量元数据。
2. 首批只支持有限家族：Python 虚拟环境及其配置、Node 项目与依赖目录、包管理缓存。其他家族返回未知；Homebrew、Conda、模型仓库等逐个扩展并各自验证。
3. 读取 manifest/lock/管理器元信息只能说明声明、来源或关联，不能证明现在无人使用；不执行项目脚本、不 import 项目包、不沿符号链接无限扩展范围。
4. 表达多对多关系：一个项目有多个环境；一个缓存/存储被多个项目共享；多个管理器下同名包未必相同。证据冲突并列展示，不能靠路径优先级强行选唯一来源。

建议的识别结果合同（具体 schema 在 ISS-034 固化）：

| 字段 | 含义 |
|---|---|
| subject_id / dataset_id / snapshot_id | 目录稳定引用与证据时点，路径在本地映射 |
| kind / detector_id / detector_version | 环境、安装依赖、下载缓存、模型文件、构建产物或 unknown |
| provenance[] / consumers[] | 管理器/安装来源/版本证据、关联项目；可为空，可多值 |
| purpose / usage_notes | 用途、一般如何使用；区分规则说明与观察事实 |
| evidence[] | 类型、来源位置、受控摘录或摘要、采集时间/指纹；不包含秘密 |
| confidence / limitations | high/medium/low/unknown、缺少哪些证据；不伪造概率 |
| impact / recoverability | 可能影响哪些消费者、能否重建、是否需要重新下载；默认 unknown |

“近期未访问”“可重新下载”“是缓存”均不能直接等价于“安全删除”。APFS clone、硬链接、pnpm 等共享存储不能按目录表相加推算收益。用户自行标记“保留”优先展示，分析结果不能覆盖该标记。

## 五、可替换 Agent Runtime

运行时是一个**可选解释服务**，不进入采集事务。离线、未配置、额度不足或分析失败时，扫描、历史和本地识别照常可用。首次不引入通用任意工具代理。

```text
有效快照/差分 + 本地识别证据
  → 用户选择范围 → 最小化/路径别名 → 请求预览
  → analysis_run → provider adapter → schema/引用/范围验证
  → 保存解释版本 → 目录标签/状态/变化解读
```

拟议最小输入：`schema_version, analysis_id, subject_ids, snapshot_ids, measurement_context, facts, evidence_ids, question, locale`。默认发送相对路径类别/别名、大小变化、识别摘要；绝对路径和 manifest 内容需额外选择，默认不发送文件正文、密钥、环境变量、客户资料。路径别名也可能暴露语义，预览必须展示实际载荷类别。

拟议最小输出：`schema_version, summary, observations[], suggested_tags[], limitations[]`。每条 observation 包含 `subject_id, statement, evidence_ids, confidence, impact, suggested_next_step`；next_step 初版只允许“查看证据/打开目录/人工确认/重新分析”等枚举，禁止 shell/delete 等执行指令。

运行记录至少保存 provider/model、输入摘要指纹、提示/规则版本、所绑定快照、开始/结束、成功/失败/取消/超时、耗时/用量（供应商可用时）、脱敏错误。密钥存系统凭据设施，不进数据库导出或日志。

必须的运行控制：

- 超时、取消、输出大小、token/费用预算与有限重试；没有真实幂等保证时不盲目重试收费请求。用户可查看与取消正在进行的分析。
- 同一事实集与版本可缓存；快照/证据变化后标记“已过期”，不得覆盖成看似当前结果；历史解释保留来源时点。
- 拒绝无效 JSON、越界 subject_id、不存在 evidence_id、超长/越权标签。原始文本可作为失败诊断，但不能进入可信状态栏。
- 文件名、manifest、Agent 返回值都按不可信数据处理，不能改变系统指令或数据读取范围。Agent 输出不能调用系统命令。
- 本地模型与远程模型走同一适配合同；是否支持具体 SDK/供应商在实现时核查版本，当前不绑定 Agent 框架。

标签建议与用户标签分栏；建议只能接受/驳回/重新分析，用户标签可撤销。用途解释、可信度、影响范围、缺失证据和更新时间必须一起显示，不能只给“可清理”绿色徽章。

## 六、智能化的准入测试

先积累匿名合成及经授权的样本集，再做供应商接入。验收以高成本误判为中心：

- venv、node_modules、共享缓存、同名异源包、符号链接、仍在使用、未知结构、冲突来源均有样本和参考证据。
- 共享/活动/未知依赖的所有关键反例不得被建议为可直接删除；不能以平均准确率掩盖此类误导。
- Prompt injection、越界标签、捏造证据 ID、超时、取消、断网、重复请求、结果过期和用户覆盖均验证。
- 无 Agent 模式可完成完整基础流程；未授权范围没有外传。标注“NOT_VERIFIED”的提供者或模型不得默认启用。

## 决策仍待实验的部分

运行时打包器、最低系统/CPU 支持、后台服务 API、端口发现方式、签名凭据、开源许可证和首个 Agent provider 均不在本 PR 中伪装成已落实。具体实验和所需用户决定已经进入任务卡，不阻塞本次规划交付。
