# ZCode 社区 CLI Worker 桥接方案

日期：2026-09-12；最后实测更新：2026-09-13
状态：源码研究、离线复现与官方远控 Start 实测已完成；机器 Worker 桥尚未实施或部署。当前实测结论以末节为准，前文保留各阶段证据。
任务源：`../TASKS.md` 的 `TASK-2026-09-12-ZCODE-COMMUNITY-WORKER-DESIGN`。

## 目标与推荐路线

把 kingsword09/zcode-cli 增强成可由 Multi-Agent Orchestration 控制、可人工接管的 ZCode Worker。以其 `RuntimeAdapter` 为接入点，增加机器控制接口；保留 TUI 供人工检查和接管。现有官方 App Server driver 作为兼容实现，二者由同一个 Worker 合同管理。

Worker 控制、套餐选择和扣费归属分别验收。更好的 TUI/控制接口不能单独证明 Start/Weekend 可用，也不能证明社区客户端享受渠道优惠。

## 基线与已验证证据

社区源码冻结在 `6ba4033aed088b60ca5e650d425622024404dd2e`，package.json 版本 `3.11.2-24`。该版本于 2026-09-12 发布准备提交，本轮没有安装 npm 包、构建或运行其真实 Agent。

1. [RuntimeAdapter 类型](https://github.com/kingsword09/zcode-cli/blob/6ba4033aed088b60ca5e650d425622024404dd2e/packages/zcode-tui/src/types.ts)：提供 `sendInput`、`submitPrompt`、`interruptTurn`、`setTransientModel`、`subscribeSessionEvents`、`readSessionUsage` 等函数；`requestPermission` 是调用参数中的回调。许多函数为可选项，必须现场检查能力。
2. [TUI 实现](https://github.com/kingsword09/zcode-cli/blob/6ba4033aed088b60ca5e650d425622024404dd2e/packages/zcode-tui/src/index.ts)：运行中输入使用 `delivery=steer_active_turn`、`queueDelivery=guide` 并绑定 `expectedTurnId`；有临时模型切换、权限等待和后台任务控制。类型存在不代表每个内核版本都实现了该能力。
3. [内核补丁计划](https://github.com/kingsword09/zcode-cli/blob/6ba4033aed088b60ca5e650d425622024404dd2e/scripts/sync-runtime.ts)：除 TUI 外还修改会话桥接、OAuth、重试、EOF 处理等逻辑。因此“只是加 TUI、官方核心完全未改”不准确。关键补丁有兼容性检查；后续仍要固定发行版、内核摘要和补丁报告。
4. [桌面迁移](https://github.com/kingsword09/zcode-cli/blob/6ba4033aed088b60ca5e650d425622024404dd2e/src/desktop-migration.ts)：按 provider family 选择一份配置，Coding Plan 得分 4，其他 `builtin:<family>-*` 得分 1；只保留 family/name/baseURL/models，未保留原 providerId、实际选中的套餐或 entitlement。写入 CLI 时使用 `provider.bigmodel` / `provider.zai`，保留已有 CLI key。
5. [配置文档](https://github.com/kingsword09/zcode-cli/blob/6ba4033aed088b60ca5e650d425622024404dd2e/docs/CONFIGURATION.md)：导入桌面设置后仍须登录；声明的接入方式为 OAuth、Coding Plan API Key、自定义 API provider。没有文档证明 BigModel Weekend 模型请求可用。
6. 对该 commit 的维护源码搜索 `requestProviderRuntimeHeaders`、`runtimeProviderHeaders`、`codingPlanSubscriptionService`、`startPlan` 等，没有发现 Start Plan 运行时验证宿主实现；`start-plan` 唯一直接命中为 prompt-preflight 测试，含义只是“不被 API-key 预检拦截”。后续已检查固定 npm 包的 vendor 内核，详见末节；没有把源码搜索未命中等同于真实请求失败。

### 离线复现

执行 `bun /tmp/zcode-project-audit.YLUQCL/community-selection-probe.ts`，exit 0。脚本仅创建无凭证 fixture，调用上述 commit 的 `detectDesktopInstallation`。

- 输入：BigModel 家族；`selectedKey=coding-plan:builtin:bigmodel-start-plan`；Start provider enabled=true，只有 Flash 模型；personal Coding Plan enabled=false，具有 GLM-5.3 模型；两者使用 `.invalid` 地址。
- 结果：导入方案选择未启用的 personal Coding Plan 及其地址和模型；方案没有 providerId 字段。fixture 原文件不变。
- 结论：当前桌面导入功能不能作为“跟随 GUI 所选套餐”的实现。这里只验证迁移语义，没有证明任何真实 provider 请求成败；网络和模型请求均为 0。

### 我们的现状

当前主目录 `scripts/zcode-worker-driver.py` 仍主要提供 create/send/read/stop/compact、偏好应答、启动时 setModel；创建会话硬编码 yolo，setModel 失败有继续使用默认模型的风险，不能作为精确扣费选路的基础。

历史提交 `fc24ba99768eb4814abd82ecf01f7307f97abdd5` 可读取，含增强事件渲染、套餐库存、运行时宿主协议与测试；当前 HEAD 不包含该提交的祖先关系，当前文件也不是该增强版。原集成 worktree 路径已经不存在；不能把历史开发完成等同于当前安装生效。

该提交的宿主桥接真实 GUI 跨进程路径仍是 `NOT_VERIFIED`。其 `applied` 状态合同可以复用，但没有实现可调用的官方 GUI 宿主端点；仅返回 `headersApplied=true` 不足以给对应 CLI 请求应用运行时配置。

## 方案比较

| 方案 | 优点 | 主要代价 | 判断 |
|---|---|---|---|
| 继续增强 App Server driver | 对现有派发改动小，可复用已有提交 | 仍须完善权限、状态和宿主服务 | 保留为兼容路径 |
| 社区 RuntimeAdapter + 机器控制接口 + TUI | 复用同一运行时的纠偏、权限、临时模型和事件；便于人工接管 | 维护社区版本和补丁兼容；须补套餐身份 | 推荐主线 |
| 转成通用模型 API 网关 | Claude Code 接入形式熟悉 | Agent 会话转模型 API 语义不等价，工具执行归属复杂；不能自动解决套餐验证 | 不作为本次方向 |

## 建议架构

```text
Multi-Agent Orchestration / Orca
  └─ ZCode Worker 控制协议（新增）
       ├─ 社区 CLI 同进程控制入口 ─ RuntimeAdapter ─ ZCode runtime
       │     └─ TUI 人工接管；输入与 PM 命令进入同一会话调度器
       └─ 官方 App Server driver（兼容实现）

套餐库存与余额观测 → 选路前检查 → 会话套餐绑定 → 实际扣费证据
                                      └─ 需要运行时验证时调用受支持宿主能力
```

控制入口必须接到持有同一个 RuntimeAdapter 的进程，单独起一个 App Server 子进程并不能控制既有 TUI 会话。第一版优先每 Worker 一个本地 Unix socket，目录仅本用户访问；不扩展远程控制。纯机器模式未来可用相同命令处理器连接 stdio，stdout 不混入 TUI 控制字符。

会话输入、TUI 展示历史、权限队列必须共享一个调度器。不能把 socket 与 TUI 分别绑定到裸 `sendInput`，否则人工与 PM 会重复提交、错判活动轮次。PM 断连不自动重启/重复发送；重连先读 session、turn 和输入处理状态。

### 最小控制能力（拟新增命令，非现成 CLI 参数）

| 能力 | 对接点及规则 |
|---|---|
| capabilities / status | 返回发行版、内核、会话、轮次及实际可用操作；缺方法就报告不支持 |
| send / steer | 使用 sendInput；明确新轮与本轮纠偏，绑定 inputId 和 expectedTurnId；区分收到、排队、已消费 |
| cancel / wait / close | 使用 abortSignal/interruptTurn 和真实终态；cancel 当前轮不代表交付完成 |
| events | 订阅会话事件；本地有界缓冲、序号与丢帧标记；重连补状态 |
| permission reply | 用原 requestPermission 回调结算，绑定会话/轮次/request；应用派发合同而非一律 yolo |
| model selection | 初次 prompt 前设置并回读；运行中改变模型/套餐只在明确安全边界发生 |
| billing status | 分别报告所请求套餐、运行时实际 provider、模型调用成功和服务端扣费归属 |

恢复应使用运行时已有会话恢复入口；不把历史 transcript 回放当成会话恢复。不伪造 Orca 的 worker_done：supervised 接入仍须遵守原 Run/Task/Dispatch 和 Delivery 验收合同。

### 套餐绑定与额度语义

每个 Worker 至少绑定：provider family、原始 providerId、连接方式、account 的脱敏标识、personal/team/start/API 模式、主模型及辅助模型、组织/项目上下文（适用时）。凭证留在 runtime 或有明确授权的认证组件，不进入任务元数据。

- 模型成功切换后、首条任务发送前回读；失败即结束本次启动，不改用个人套餐或充值余额。
- main/lite/后台 Agent 都要检查；不能只锁主模型，而让标题生成或辅助请求走另外的 provider。
- 库存保留 Start Plan 下每个活动 plan_id 及模型桶、有效期和计量单位；不得把名称固定为 Weekend 或额度固定为 3 亿。
- 同一账户同一 Coding Plan 经 Claude Code 和 ZCode 调用时通常消费同一订阅池；以服务端标识确认共用池后合并预算和并发约束，不能当两份额度相加。
- “渠道优惠”是单独的服务端计费属性；不把它建成凭空增加的余额。仅有客户端名称或请求成功不能证明优惠。
- 若服务端没有 plan_id 选择参数，最多保证选中 Start provider；Start 与 Weekend 哪个桶被抵扣，由官方账单或有明确归属的余额变化确认，不能承诺指定某一活动优先扣费。
- 库存可见、entitled、认证有效、模型可用、实际请求成功分别记录；被看见的套餐不可直接加入自动派发候选。

## Start/Weekend 的独立验证路径

社区 TUI 的 OAuth 回调桥解决登录过程，不等同于官方 GUI 的逐请求验证服务。下一阶段必须检查所固定内核在 terminal 模式下是否具备完整、受支持的 Start provider 请求路径；若存在，用它建立真实模型请求。若仍发出需要宿主处理的运行时请求，则需对接能给同一个 Worker runtime 应用配置的宿主能力。

目前尚未证明有该官方宿主接口。需要人工验证时报告 `needs_user_verification`；宿主不可达时报告 `host_unavailable`；不将无法完成的验证回复成成功，不保存或重放验证码票据。保持其他已可用 Worker 通道正常派发。

## 实施顺序与验收

1. 恢复现有成果基线：从历史 commit 对目标文件逐项审计并迁移，与当前主线新规则做 scoped 集成；先解决 setModel 失败静默回退、敏感输出和权限默认值。不能直接整分支覆盖主线。
2. 社区 Worker 接入：实现共享会话控制器、socket 控制接口与 MAO 适配；在独立 checkout / 配置范围运行。保留现有 zcode 入口，绑定明确发行版路径；先用 fake RuntimeAdapter 证明重复输入、断连、跨轮纠偏、权限答复和事件丢失行为。
3. Coding Plan 小规模实操：在隔离测试 worktree 完成一项有实际产物的修改，运行中纠偏一次、经过一次权限答复，观察真实最终结果及资源退出；两 Worker 的不同模型不互相污染，也不覆写 GUI 选择。绑定固定 head 后由不同 GLM Worker 独立验收。
4. 套餐与渠道验证：复用中立库存合同和 token-monitor 的已归一化余额结果；在不存在其他同池调用或能按请求归属核算的条件下，记录调用前后余额、官方账单和报告延迟。粗粒度百分比/缓存/并发/重置导致无法归因时标记未知，不能为制造差额重复大量请求。
5. Start/Weekend：同一固定版本完成 modelRef 注册、登录、运行时验证、模型输出，并取得目标额度桶扣费证据后才允许自动派发。协议 fixture 通过不替代真实验收。渠道优惠与 Start 消费均为独立通过项。

执行实现任务时遵守用户指定的 Multi-Agent Orchestration 派发方式：GLM Worker 在独立 worktree 实施；纯研究没有独立实现交付时由 PM 完成。本次研究未派 Codex subagent。

## 09-12 研究阶段结论与未验证范围

- 社区 CLI 的 RuntimeAdapter 是有源码依据的 Worker 改造入口。
- 已离线复现“桌面选中 Start、迁移却选择 personal Coding Plan”的语义缺口。
- 已定位可复用历史增强版 driver 提交，当前主目录尚未采用。
- 控制接口、真实 TUI 接管、真实 Coding Plan 扣费、社区渠道优惠、Start/Weekend 模型请求和 GUI 跨进程宿主验证：全部 `NOT_VERIFIED`；本轮没有实施这些能力。
- 本地仅新增研究档案与更新任务卡（均为 ignored 资料）；未改变运行中的 Worker 或账号配置。

补充来源：[ZCode 官方连接模型与套餐文档](https://zcode.z.ai/cn/docs/configuration)、[token-monitor 套餐查询 PR #630](https://github.com/Javis603/token-monitor/pull/630)。前者区分套餐和资源包端点；后者只证明额度发现/查询，不证明社区 CLI 的推理访问能力。

## 09-12—13 续查：Start Plan 宿主依赖与官方远控路径

对应任务：`TASK-2026-09-12-ZCODE-START-HOST-AUDIT`。本节是固定版本源码证据与离线边界验证，不是已实现的 Worker backend。

### 固定发行包核验

- 本机官方：ZCode 3.11.2 / CLI 0.16.5；`/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs` 的 SHA-256 为 `e9f1868c0fdb863537ed910ee3828b9be96b8c2fd805473f63b439e1113266b8`。
- 社区：从 npm 获取 `zcode-app-cli@3.11.2-24` tarball，15,637,221 字节；SHA-1 `524a367f212a7c28ea74bc40d24190b4891abbdc` 与 npm dist 元数据一致；SHA-512 SRI `sha512-bydWsiRAh8+vuuaXFuIE+LC4O7kxpMyo3mp5u9tOOklxFq0XMzrV4PEpGCdsh5px4nkWUUE0mwlLEpMRVYx99Q==` 亦匹配。
- 只在 `/tmp/zcode-start-audit.5YLDTo` 解出核查所需的三个普通文件；没有 npm 安装、运行安装脚本、替换 PATH/官方软链、启动完整社区 runtime 或导入真实登录状态。
- `vendor/extraction.json` 声明从官方 3.11.2 Linux deb 提取 CLI 0.16.5，14 项补丁 applied；vendor 内核 SHA-256 为 `6931ad1df3063d9c428edb5b43d8be32c677174b45f2610e8c7e88514dbfbc45`。声明的提取来源和补丁报告不等于本轮重做构建；本轮只独立核对实际发行包。

### 已确认的连接层次

| 入口 | 当前证据 | 能否据此宣布 Start 已通 |
|---|---|---|
| 外部独立 App Server | `XMe` 注入 `$6i`；BigModel / Z.ai Start provider 请求运行时头；未应用时抛出 `-32031` | 不能，仍需调用方提供真实宿主服务 |
| 官方/社区直接 TUI | `H_n` 的 `createZCodeApp` 参数没有注入 `providerRuntimeHeadersPort`；社区保留与官方逐字节相同的 `$6i` 实现 | 不能；没有 hook 不代表服务端接受，也没有发现等价终端验证实现 |
| 桌面 GUI | renderer 的 `PJt → Yat/Jat` 处理工作区验证请求；host `respondProviderRuntimeHeaders` 给该请求持有的 `Z.client` 应用 runtime model，再回复结果 | 有完整宿主实现的静态证据；本轮没有真实调用 |
| 官方 Remote Control | main `JV/createWebRemoteControlSharedHostAttachments` 把 MessagePort 接到 `windowHostProcessMap` 已有 host；host 从 `er` 取同一 local services，再暴露服务 | 是最值得实测的宿主复用候选，不是已经打通的公开 Worker API |

桌面 renderer 的 `RJt → LJt → PJt` 对已登记的 workspace tabs 安装监听，不只绑定当前输入框。host 的动态订阅还会补发已等待的验证请求。由此可以提出较强的架构假设：**依赖的可能是桌面宿主，而非用户必须在桌面输入框手动发送。** 但窗口状态、工作区订阅、远控配对和实际验证交互仍可能影响运行，必须实测。

### 官方公开入口及限制

[Remote Control 文档](https://zcode.z.ai/cn/docs/remote-control) 明确允许从手机向桌面已有工作区发指令、创建任务；电脑保留原有执行环境，桌面必须运行且联网，仅一个手机页面可连接。连接链接携带授权，不能公开记录到任务、日志或报告。官方文档没有公开承诺通用 SDK、无头机器控制协议或 Start Plan 支持。

[Bot Channel 文档](https://zcode.z.ai/cn/docs/bot-channel) 描述微信/飞书绑定后创建任务、切模型与回传决策，也操作桌面已有会话。它提供另一条官方外部输入途径，但本轮未绑定账号、创建机器人、发送外部消息，也不把机器人能力类推成 MAO 可直接使用。

本轮没有启用 Remote Control、读取配对密钥、连接真实中继、注入 Electron、操作 GUI 或转发验证码票据。`AttachServicePort` 是桌面内部经父子进程传递 MessagePort 的机制；发现这个消息名不等于外部进程已经具有可调用的 attach 端点。

### 离线探针与复核

执行以下两个命令均 exit 0，各通过 7 个检查：

```sh
node /tmp/zcode-start-audit.5YLDTo/host-boundary-probe.cjs
node /tmp/zcode-start-audit.5YLDTo/host-boundary-probe.cjs /tmp/zcode-start-audit.5YLDTo/package/vendor/zcode.cjs
```

探针从固定官方/社区文件提取 `$6i` 以及本机桌面 `JV` 函数，在无 `process`/`require` 的独立 VM 上下文中使用内存 fake dependencies 执行；不加载整个 Agent。检查两类 Start 要求刷新、Coding Plan/custom 不走该特定 hook、负应答返回 `-32031`、远控复用已有 host、缺 host 失败、远程工作区要求身份、端口释放幂等。未伪造正验证答复；探针网络请求 0，真实凭证读取 0。两次都复用同一桌面 main 函数，不代表独立验证了社区 GUI。

另做静态提取比较：官方与社区 `$6i` 函数字节相同；两份 `H_n` 均不含 `providerRuntimeHeadersPort`。这不是完整 CLI / TUI / GUI E2E。临时脚本与包保留在上述目录便于复查，可能被系统临时文件清理机制移除。

ASAR 证据（偏移为 UTF-8 解码后 JavaScript 字符索引，不是行号）：

- `out/main/index.js` SHA-256 `f212ebfcc743cab4a73a194c50de53e4433036a81479daed7b727450c774bf5a`：`JV` 附近 1345xxx；远控调用 `attachWorkspaceHost` 约 1167316。
- `out/host/index.js` SHA-256 `30911a90dadc5c384959d00d95ccc70c8cf38c74a9cb99c3168b0897d046d215`：响应应用配置约 381352；共享 `er` 服务解析约 2296104；AttachServicePort 处理约 2304912。
- `out/renderer/assets/styles-DyAcaLKy.js` SHA-256 `d167059e9a6f4cb355cedac0a732725afdc57a8d0c84b89e3ede48f41d5945ac`：验证处理约 2298260；工作区监听约 4585360。

### 下一步与停止边界

建议保留两条不同用途的路线：社区 RuntimeAdapter 改造提升独立 Worker 控制；官方 Remote Control 探针验证能否复用桌面宿主来使用 Start。这不是将所有 Worker 都改造成远控会话的决策。

真实宿主测试的新增前提：用户手动开启官方 Remote Control，并明确授权测试会话范围；不要求用户把带授权链接公开贴进对话。随后先验证配对、限定工作区和会话、读取实际 provider/model，再发一条最小请求；若触发人工验证则由用户在官方界面处理。必须确认主/辅助模型无套餐回退，并取得可归因的账单或额度桶变化，才可宣称 Start/Weekend 消费成功。没有官方机器接口可用时，记录 `unsupported_control_surface`，不把内部消息名当现成外部 API。

研究/离线核查已完成；真实远控、运行时验证、Start/Weekend 请求与扣费、MAO 控制桥实现均为 `NOT_VERIFIED`。当前没有可证明足够的授权接入信息，因此本轮没有发起真实计费请求。

## 09-13 实测更新：官方远控已连接，Start 请求尚未发送

用户授权代开远控并自动测试；经告知授权覆盖当前窗口已登记工作区后再次明确“允许”。通过 ZCode 官方“移动端远程控制”入口启用，在 Safari 无痕窗口访问官方 `zcode.z.ai` 远控页，桌面显示“手机已连接”，网页显示相同任务和工作区。这已将“官方远控连接”从假设推进为真实 UI 验证，不代表外部脚本/MAO 已可调用 RPC。

远控中可新建无项目会话草稿、打开模型菜单和官方模型管理页。当前模型管理页显示 BigModel“个人套餐”/GLM Coding Pro，另有“切换至 体验套餐 × 1”按钮。未点击切换、未修改全局模型配置、未发送任何模型请求；不能因为候选里有 Flash 就认为绑定了 Start/Weekend。体验套餐具体活动、运行时验证、主/辅助模型选路、输出和额度抵扣仍 `NOT_VERIFIED`。后续临时切换全局套餐需先确认，避免影响用户当前选择。

过程中 Mac 及 Safari 无痕窗口锁定，由用户自行解锁，未绕过。一次 AX 输出对无 scheme 的地址脱敏遗漏，已立即通过官方“刷新二维码”确认使旧链接失效，并观察到原网页断开；随后使用新链接重新配对，后续过滤整个 URL/地址栏行。报告、任务卡不保留授权链接或令牌。

收口实证：桌面点击“停止”后提示“已关闭 Web 远程控制”；网页提示“电脑端已经断开连接，当前手机页面不能继续控制桌面工作区”。本轮 Safari 无痕标签已关闭，原有起始页保留。当前没有本轮继续开放的远控连接。

用户所说“通过 HTTP remote control”应区分：官方 HTTPS 网页入口已实测；本地源码表明持续控制使用中继传输和 RPC、复用桌面宿主；未证明存在公开稳定的 REST API 或已完成无 GUI 的 Worker 适配。下一阶段仍以正式配对、会话/工作区限制、运行时验证原样保留为前提。

## 09-13 实测推进：Start 请求/扣费已通，CLI 控制边界已测

### 范围与身份

用户进一步允许临时切换体验套餐、开启远控做最小请求，结束恢复个人套餐并关闭远控，并要求多收集后续 Worker 控制迭代证据。按 MAO 研究/验证边界由 PM 实测，没有派 Codex subagent、实现 Worker 或修改正式 driver。

- 运行时：本机官方 ZCode 3.11.2 / CLI 0.16.5，内核摘要仍为 `e9f1868c0fdb863537ed910ee3828b9be96b8c2fd805473f63b439e1113266b8`。
- 官方模型页临时选择“体验套餐”：活动 `ZCode Weekend Build`，仅 `GLM-5.3-Flash`，起始今日余额 `300,000,000 / 300,000,000`，到期 `9月14日 09:00`。这些是本次账户的观测，不能硬编码为所有 Start Plan 的定义。
- 从远控创建无项目会话，标题“官方远控最小连接探测测试”；session ID：`sess_1045b7b8-50f9-4a50-a74b-7a338d8e13dc`，底层版本 `0.16.5`，模式 `build`（UI“变更前确认”），推理强度低。未修改旧任务或项目文件。
- 首条请求明确禁止工具、文件访问、联网检索及配置修改，只要求返回固定探针标记。

### 真实控制证据

| 检查 | 实际观察 | 边界 |
|---|---|---|
| 套餐绑定与输出 | 首条返回 `ZCODE_REMOTE_START_PROBE_OK_20260913`；主回复及自动标题的 model_usage 均为 `builtin:bigmodel-start-plan / GLM-5.3-Flash` | 证明本次官方桌面宿主路线，不证明独立 CLI Start 路线 |
| 准入/消费 | session_input 记录 `kind=sendText, delivery=startNow, status=promoted` | 不能把键盘/点击已发出等同于输入已消费 |
| 同会话续传 | 第二条不重述旧标记，要求回忆上一条；正确返回旧标记及 FOLLOWUP 标记，session ID 不变 | 验证跨轮上下文；未测试活动轮内 steer/guide |
| 网页刷新 | 刷新后恢复同一任务及两轮历史；后续准入计数符合实际提交数，无重发副本 | 只覆盖一次网页重连，不是桌面/CLI 进程重启恢复 |
| 停止当前轮 | 第一轮取消探针在操作前自然完成，记为未命中取消窗口；第二轮在“停止生成”出现后立即点击，UI“已停止”，model_usage `status=cancelled, cancelled_by_user=1` | 以真实状态验收；模型正文声称“已停止”没有证据权威 |
| 停止后恢复 | 相同 session 再次返回 `ZCODE_REMOTE_RESUME_OK_20260913` | 证明 stop-turn 不等于 close-session |
| 工具与权限 | 整个会话记录工具调用 0；没有人工验证码弹窗 | 没有测试权限请求或验证码出现时的无人值守可用性 |

初次点击发送后，草稿保留、无会话/回执；只读查询当时没有匹配探针的 task/session_input。随后输入框键盘提交成功。因此撤回“无项目远控不支持”的临时假设：无项目会话实测可用。精确的点击/键盘/输入框时序原因未定位；不能把一次无反馈当作协议拒绝，也不能不查回执就连点重发。另一轮提交后首帧仍显示草稿，稍后实际已完成，进一步说明 UI 快照存在时序差，不能自动重复提交。

### CLI 只读探针：共享用量不等于拥有活动会话

临时脚本：`/tmp/zcode-live-control.Jz29jj/read-existing-session.cjs`。命令 `node /tmp/zcode-live-control.Jz29jj/read-existing-session.cjs` 两次 exit 0；第二次补输出确定的 usage 数字。每次依次启动官方内核的 `app-server --surface terminal` 和 `app-server --surface desktop`，只发送 `session/read`、`session/usage` 到上述 session。

| 方法 | terminal | desktop |
|---|---|---|
| `session/read` | `-32004 Session is not active` | 相同错误 |
| `session/usage` | `totalTokens=17418, modelRequestCount=2, modelErrorCount=0` | 相同统计 |

探针时点为首轮及标题已结束、续传尚未发送。这证明两种独立 App Server 没有通过 surface 参数自动附着到该桌面会话；不把该错误解释成所有可能的 attach/resume 路线都不支持。usage 的成功只能证明持久化统计可读，不能拿它冒充活动状态或控制权。

四个临时子进程均 exit 0，无超时、stderr、反向交互请求或通知；CLI config 读前/读后摘要一致。没有发出 create/resume/send/setModel/stop/close 等会话变更请求，没有替换入口或同步凭证；未使用不安全的旧 driver 启动默认 yolo 会话。临时脚本不进入发布入口。

### 用量与官方额度：首轮精确对账，全轮有未解释差额

该 session 的本地 model_usage（所有已记录模型均为 Start/Flash）：

| 请求 | 状态 | 本地总 tokens | 说明 |
|---|---|---:|---|
| 首轮主回复 | completed | 16,912 | 固定探针标记 |
| 自动标题 | completed | 506 | 辅助调用也走 Start |
| 上下文续传 | completed | 16,984 | 正确保留上轮内容 |
| 第一次取消探针 | completed | 17,987 | 未实际停止，不算取消成功 |
| 第二次取消探针 | cancelled | 0 | `provider_total_tokens=null`；不能解释成服务端免费 |
| 停止后恢复 | completed | 18,179 | 同一 session 成功回复 |
| 合计 | 6 行 | 70,568 | 5 条准入输入、工具调用 0 |

首轮主回复与标题合计 `17,418`，官方页刷新后余额为 `299,982,582`，差值精确匹配。这是 Start/Weekend 实际消费的直接证据，不再只是库存可见或推理路由推测。

全轮最终官方页刷新为 `299,891,996 / 300,000,000`，累计下降 `108,004`，与本地合计相差 `37,436`。该差额尚未归因。只读聚合在本地数据库所查窗口（2026-09-13 01:05 起）未见其他 Start 会话；本 session 的记录均 `attempt_index=0, retry_count=0`。本地数据库不是完整服务端账单，因此这不能排除未落本地账、其他客户端或服务端计量因素。

关联日志中观察到 8 次该 session 的 provider runtime headers 请求，均为 Start/Flash，而 model_usage 为 6 行。除可对应的请求外，01:19:52.489、01:22:11.719 各有一次发生在主请求完成之后、同 turnId 的刷新请求。这是后续排查线索，不足以证明额外模型调用、重试或 37,436 差额的具体来源。只输出时间、模型、turnId 和字段名，未保存实际 headers/票据。

后续预算与取消结算不能仅依赖 `session/usage` / SQLite 总和。必须允许“本地已报告用量”和“官方余额变化”并存，并把无法归因的差额显式保留；首轮精确对账不能扩大成全轮统计完整性证明。

### 对后续 Skill 迭代的约束

1. 明确区分“官方桌面宿主控制”“独立 CLI/App Server”“持久化观测”。机器客户端必须证明控制的是具名 runtime/session，不能靠共同数据库路径推断附着成功。
2. 输入流程需区分草稿已填、提交动作发出、服务端准入、实际消费；重连或超时后先读身份/输入回执，不自动重发。
3. 套餐要绑定完整 providerId/modelId，覆盖已观察到的主调用和辅助调用；全局选择与会话选择分开建模。会话级套餐隔离、并发模型互不污染尚未实测。
4. stop-turn、close-session、断开远控、杀进程是不同操作；本次只证明 stop 后同 session 可继续及远控可关闭。
5. 保留官方运行时验证链。没有验证码弹窗不代表无需验证，也不保证未来不会触发；请求头刷新在日志中有实际记录。
6. 本次没有公开稳定 HTTP API/无浏览器 RPC 客户端、活动轮内 guide/queue、真实权限应答、桌面重启恢复、社区 TUI 机器控制或 MAO supervised `worker_done → Delivery` 的成功证据，均为 `NOT_VERIFIED`。

新增缺口统一登记于 `TASK-2026-09-13-ZCODE-CONTROL-FOLLOWUPS`。正式 Skill 脚本/版本未更改，也未声称后台 Worker 已可直接调用 Weekend 额度。

### 最终收口

通过官方模型页恢复“个人套餐”/GLM Coding Pro；桌面停止远控后显示“已关闭 Web 远程控制”，远控网页确认“电脑端已经断开连接，当前手机页面不能继续控制桌面工作区”；关闭本轮 Safari 无痕标签。测试任务及只读统计保留作证据。无测试监听器、无未退出 CLI 探针进程；不删除用户既有任务或文件。

## 09-13 CLI 桥实现前置：能力探测与社区共享调度器

第一波 MAO Claude Code / GLM-5.3 Worker 已按 `TASK-2026-09-13-ZCODE-DRIVER-SAFETY` 在独立 worktree 交付草稿 PR #147（`6a04635b60784ce3dc5cd8c10153d2b7d87e44f5`）；当前仅 driver 基础修复，不算社区控制桥或 Start 请求接入完成。PM 定向复跑 27+59 项断言通过，同合同 postflight 通过；独立 reviewer 与真实官方入口仍未验收，不合并/部署。本轮终端、lease、Delivery 已结算，未合并 worktree/分支保留、监督 heartbeat 暂停。运行身份、证据格式纠偏及资源收据以任务卡为准，不在此重复维护。

### 官方配置参数：help 与实际解析不一致

固定官方 bundle `0.16.5`，SHA-256 `e9f1868c0fdb863537ed910ee3828b9be96b8c2fd805473f63b439e1113266b8`。PM 执行 `node /Applications/ZCode.app/Contents/Resources/glm/zcode.cjs app-server --settings /tmp/zcode-nonexistent-synthetic-settings.json --help`，退出 1，输出 `Unknown option '--settings'`；虚构路径没有被创建，不发送模型请求。

静态证据：`S$i` 的 `parseArgs` 选项没有 `settings`；`z$i`（`runZCodeProtocolCommand`）传递 cwd/env/io/surface/version，但没有 `userConfigPath`。`tI()` 经 `ws(T_r)` 解析默认配置，`T_r="~/.zcode/cli"`、`I_r="config.json"`；`Nee()` 支持函数参数传入路径不等于命令行暴露了该参数。现有 driver 的 `ZCODE_CLI_CONFIG` 不被该 bundle 识别。

因此不能直接追加 `--settings`、只用支持它的 stub 测试，再声称本机官方隔离已实现。后续实现需找到真实支持且可独立验证的入口；若该发行版不支持，应明确能力失败并停止准入，不偷偷回落共享配置。不覆盖应用 bundle，不修改 HOME/全局配置来凑测试。

### 社区控制面需要复用输入调度，不只是 RuntimeAdapter 名称

固定社区 `6ba4033aed088b60ca5e650d425622024404dd2e`。`packages/zcode-tui/src/types.ts` 的 RuntimeAdapter 暴露可选 `sendInput`、`interruptTurn`、`setTransientModel`、projection/usage 和事件订阅；输入选项包含 `inputId`、`expectedTurnId`、`queueDelivery`、reservation/pendingInput 标识与 permission callback。

但输入队列及 activeTurnEpoch/activeTurnId 控制仍在 `src/index.ts` 的 ZCodeTui 私有实现中，`runTui` 只是创建类并运行。机器接口不能与 TUI 各自绕过调度器裸调 sendInput；需共用一个准入/去重/排队控制器，区分 transport received、runtime admitted/consumed、turn completed。`scripts/sync-runtime.ts` 还按上游锚点补充 bridge 行为，因此它不是完全不修改 runtime 的纯界面替换；后续改造要锁版本、验证锚点并失败关闭。

上述代码路径不包含官方 Start 宿主验证的可用性证明。共享 controller、官方宿主机器连接、真实权限答复及 MAO 完成回执仍为 `NOT_VERIFIED`。
