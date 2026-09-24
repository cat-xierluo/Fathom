# ZCode CLI（`zcode`）Worker 可行性研究（本地归档草稿）

> 本文件含本机环境观测与尚未验证的产品推断，仅保留在被 Git 忽略的本地归档中，不属于公开 Skill 交付，也不作为当前 backend 支持声明。

> 本文档为 SKILL.md 的补充参考文档，记录智谱 ZCode 桌面端内嵌 CLI 作为 worker backend 的可行性研究。
> 研究日期：2026-08-27
> 前提：本机已安装 `ZCode.app`（智谱桌面 Agent，Electron 应用），版本 3.9.2，引擎 CLI 版本 0.16.5。

---

## 1. 概述

ZCode 是智谱的桌面 Agent 应用（对标 Claude Code / CodeBuddy），内置一个**可独立运行的 CLI**（`zcode`），功能对标 Claude Code，可以作为 multi-agent orchestration 的 worker backend 使用。

核心价值有三个，按重要性排序：

1. **天然吃 0.66x 额度折扣（总额度 ×1.5）**：智谱官方对「在 ZCode 中使用 GLM Coding Plan」提供限时 1.5 倍额度加成（1/0.66 ≈ 1.5）。CLI 走 ZCode 自家引擎的请求路径，`x-client-sig` / `x-client-pow` / 错峰票等客户端身份签名由引擎**自行计算**，无需逆向或仿造指纹——这是相比自建网关上游的最大优势（做网关反而要抓包逆向、验证扣费，得不偿失）。
2. **零配置复用登录态与额度池**：CLI 自动继承 ZCode 桌面端的认证和 GLM Coding Plan 额度，无需额外配置 API Key。
3. **原生复用本机 Skills**：CLI 原生扫描 `~/.agents/skills/`（`user/agents` 来源），本机 21 个核心技能（`agent-browser`、`browser-use`、`computer-use`、`orca-cli`、`orchestration`、`frontend-design` 等）已在那里，无需开发 extension 即可在 ZCode TUI 内使用。

> ⚠️ **与自建网关上游的取舍（用户已决策）**：本仓库另有 `zai_upstream.py` 的 AutoClaw 上游设计（DEC-013），但那是针对 AutoClaw（智谱 OpenClaw fork）。对于 ZCode 本身，**直接使用其内嵌 CLI 即可吃到折扣**，不需要再做一个网关上游去仿造 ZCode 客户端签名。除非未来 ZCode 关闭 CLI 或要求强校验 `x-client-sig` 时，才回退到网关仿造方案。

## 2. 二进制位置与安装边界

CLI 不是一个独立安装的二进制，而是 ZCode 桌面端打包的 Node 可执行脚本。默认只使用已存在的 app bundle 绝对路径；本节不授权 worker 安装桌面端、写 `~/.zshrc`、创建 symlink 或修改机器环境，这些动作必须由用户明确批准精确命令。

| 属性                     | 值                                                          |
| ------------------------ | ----------------------------------------------------------- |
| 可执行脚本路径           | `/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs` |
| shebang                 | `#!/usr/bin/env node`（系统 node 即可，无需额外运行时）      |
| 当前版本                 | `zcode 0.16.5`（随桌面端自动更新）                           |
| 引擎本质                 | Node bundle（约 12 MB），含 TUI / `--prompt` / `app-server` |
| 认证/配置目录            | `~/.zcode/`（含 `cli/`、`v2/`、`skills/`、`agents/` 等）    |

### 2.1 版本验证

```bash
/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs version
```

### 2.2 PATH 链接（已完成：2026-08-27）

CLI 嵌在 app bundle 里，`which zcode` 默认报 `not found`。**本次集成已创建软链**到用户 PATH 里的 bin 目录（用户明确批准），终端可直接敲 `zcode`：

```bash
# 已执行的软链（目标：/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs）
ln -s "/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs" "$HOME/.local/bin/zcode"

# 验证（实测通过 → 0.16.5）
zcode version
```

- `~/.local/bin` 已在用户 PATH（第 5 位），软链后 `zcode` 全局可用。
- 备用方案（若未来软链被清理或换机器）：`alias zcode="/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs"` 写入 `~/.zshrc`。
- `zcode.cjs` 已带 `#!/usr/bin/env node` shebang 且系统 node 可用（实测 v22），软链后**直接可执行**，无需 chmod 或 wrapper。

> ⚠️ 依赖桌面端存在：软链指向 app bundle，若用户卸载 ZCode.app 则软链失效。PM/worker 应先 `zcode version` 探活。

## 3. 命令行用法

```
用法: zcode [command] [options]

不带子命令时打开全屏 TUI。

命令:
  app-server   Run the ZCode Protocol stdio app server
  commands     List custom slash commands (`commands list`)
  doctor       Inspect runtime and packaging assumptions
  login        Sign in with Z.AI OAuth for model access
  logout       Remove the shared Z.AI login credentials
  plugins      List and enable installed plugins (`plugins list`)
  skills       List local skills (`skills list`)
  tui          Open the terminal UI
  version      Print the CLI version

选项:
  -h, --help        Show help
  -v, --version     Show version
  --prompt <text>   Run a single prompt without opening the TUI
  --browser-use <mode>  Enable Browser Use backend (supported: headless)
  --surface <surface>   Presentation surface for headless prompts/app-server: terminal or desktop
  --browser-executable <path>  Chrome/Chromium executable for headless Browser Use
```

### 3.1 启动方式（用户最关心的）

> ⚠️ **2026-08-27 实测：裸 `zcode`（TUI 模式）独立运行会报错**——
> `Cannot find package '@zcode/tui' imported from .../glm/zcode.cjs`。
> 原因：TUI 依赖 `@zcode/tui` 包，它不在 app bundle 的 `glm/node_modules`（该目录不存在），
> 也不在文件系统（`zcode-tui-runtime/` 搜不到）。桌面端能跑 TUI，是因为 App 运行时
> 通过 SEA/Electron 机制注入了 TUI runtime；独立软链的 `zcode.cjs` 没有该环境。
> **结论**：`zcode` 裸命令、`zcode tui` 无法独立用于交互 TUI（除非以后能找到注入 TUI runtime 的方法）。
> 但 **`--prompt` / `app-server --stdio` / `doctor` / `skills` / `plugins` 均不依赖 TUI，可独立运行**。

| 启动方式                 | 命令                                            | 独立运行？ | 适用场景                                     |
| ------------------------ | ----------------------------------------------- | ---------- | -------------------------------------------- |
| **交互 TUI**             | `zcode` / `zcode tui`                           | ❌ 缺 `@zcode/tui` | 仅桌面端 App 内可用；独立 CLI 不可用        |
| **单次任务（headless）** | `zcode --prompt "任务描述"`                     | ✅         | 批处理、脚本化、worker 无头执行              |
| **多轮会话（headless）** | `zcode --prompt "..." --resume <id>` / `-c`     | ✅         | 完整 agent session：追问/纠偏/多轮（见下）  |
| **stdio backend**        | `zcode app-server --stdio`                      | ✅         | 作为 agent 子代理/网关 agent 后端（见 §3.3） |

> 需要 `~/.zcode/cli/config.json` 配置 model provider 后，`--prompt` 才可用（见 §4 认证）。

#### 会话模型：`--prompt` 不是只能一次性（2026-08-27 逆向参数表确认）

`--prompt` 模式的完整参数表（引擎 `parseGlobalArgs`，strict 模式）：

```text
-p/--prompt <text>     单次任务文本
--resume <id>          恢复指定会话（同会话续接）
-c/--continue          继续最近会话
--json                 结构化 JSON 输出
--output-format <fmt>  支持 "json" / "stream-json"（stream-json = 轮内流式事件）
--attach <file>        附件（可多次）
--cwd <dir>            工作目录
--mode <mode>          模式
--target <req>         target request（headless 指定目标）
--stdio                stdio 结构化 IO
--surface <surface>    terminal / desktop（headless 必须 terminal）
```

**组合出完整 agent session**（对标 `claude -p --output-format stream-json --resume`）：

```bash
# 第 1 轮：建会话（--json 拿 session id）
zcode --prompt "开始任务..." --json
# 第 N 轮：同会话追问/纠偏（上下文保持）
zcode --prompt "把第 2 步改成..." --resume <sessionId>
# 或直接续最近会话
zcode -c --prompt "继续"
# 轮内流式监测（PM 实时看进度）
zcode --prompt "..." --output-format stream-json
```

- **多轮交互**：`--resume`/`-c` 保持会话上下文，可追问、纠偏、补充指令——语义上是完整 agent session。
- **状态监测**：`--output-format stream-json` 输出轮内流式事件（不止"跑完看结果"）。
- **编排接入**：与 orchestration skill 驱动 `claude -p` / `codebuddy -p` 的模式同构，spawn-worker 可直接套用。
- 更强的程序化面（`session/send` 多轮 + `session/events` 订阅 + `session/fork`）在 `app-server` 协议里（§3.3），但 CLI 层的 resume 模式已够编排用，无需逆向协议。

### 3.2 子命令速查

| 子命令                | 说明                                    | 独立运行 |
| --------------------- | --------------------------------------- | -------- |
| `zcode tui`           | 打开终端 UI（等价不带参数）             | ❌ 缺 TUI runtime |
| `zcode app-server`    | 运行 ZCode Protocol stdio app server    | ✅       |
| `zcode login`         | Z.AI OAuth 登录获取模型访问权限          | ✅       |
| `zcode logout`        | 移除共享的 Z.AI 登录凭证                 | ✅       |
| `zcode doctor`        | 检查运行时与打包假设                    | ✅（`sea: no`，node-bundle 形态）|
| `zcode skills list`   | 列出本地可用的 skills                    | ✅       |
| `zcode plugins list`  | 列出并启停插件                            | ✅       |
| `zcode commands list` | 列出自定义 slash 命令                    | ✅       |
| `zcode version`       | 打印 CLI 版本                            | ✅       |

### 3.3 `app-server` 协议性质与接入判定（worker 深度研究结论）

`zcode app-server --stdio`（同义词 `agent-server`）启动一个 **JSON-RPC over stdio 协议服务器**，
驱动一个完整的 ZCode 编码 Agent。**这是 MCP + 专有 session 协议的混合体**，走 LSP 式 stdio 帧
（`Content-Length` 头 + JSON body，含 `jsonrpc` 字段），**不是 OpenAI 兼容 HTTP 端点**。

**完整协议面**：
- **标准 MCP 风格**：`initialize` / `notifications/initialized` / `ping` / `shutdown`、`tools/list` / `tools/call`、`resources/*`、`prompts/*`、`completion/complete`
- **ZCode 专有 session 命名空间**（模型驱动核心）：`session/create` / `read` / `list` / `resume` / `fork` / `close`、`session/send`（投递 prompt 的核心方法）、`session/events` / `event` / `subscribe`、`session/messages` / `usage` / `subagents`、`session/stop` / `compact` / `goal` / `setModel` 等
- **workspace 命名空间**：`workspace/generateText` / `cancelGenerateText`（一次性文本生成）、`workspace/upsertModelProvider` / `readState` / `setDefaultModel` 等
- **automation 命名空间**：`automation/create` / `list` / `update` / `delete`

**上游路由**：通过 `~/.zcode/cli/debug/model-io-*.jsonl` 日志确认，模型运行时内部路由到
`builtin:bigmodel-coding-plan`（智谱 GLM，模型 GLM-5.2/5.3），走 ZCode 自己的登录凭据；
请求体是 ZCode 自定义内部格式（含 `thinking` 预算、`output_config.effort`、`cache_control`），
非 OpenAI chat/completions SSE。ZCode 有自己的模型抽象层（`createModel`/`generateText`/`streamText`）。

**对 gateway / Claude Code / Codex 的接入判定**：
- ❌ **不可作「直连 OpenAI 兼容上游」**：ZCode 不暴露任何 HTTP `/v1/chat/completions` 或 `/v1/models` 端点。
- ❌ **不可作「纯 `workspace/generateText` 补全后端」**：返回 ZCode 内部格式，仍走 ZCode 登录+上游计费，不能当网关多路复用上游。
- ⚠️ **理论可行但工程代价高的路线**：写适配器守护进程（stdio 子进程）起 `zcode app-server --stdio`，用 `session/create`+`session/send`+`session/events` 驱动，再把事件流翻译成目标协议。但这本质是「把 ZCode 当 agent 子代理」而非「当 model」。
- ✅ **正确语义**：`app-server` 是 **agent/子代理后端**，可用 stdio MCP 客户端包装（有 `tools/list`/`tools/call`/`resources/*`/`prompts/*` 等 MCP 兼容面），但 session 控制需走专有方法。

**关键坑**：
1. 需先 `zcode login`（Z.AI OAuth 共享凭据）；无凭据 `session/send` 等会失败。
2. `session/send` 单会话串行，重复 prompt 报错 -32010（"A prompt is already running for this session"），需按会话串行或 `session/fork` 并发。
3. 无 HTTP 端点，不可远程直连；`--surface desktop` 会拉起 GUI，headless 需显式 `--surface terminal`。

## 4. 认证与额度

CLI 支持两种认证方式，均可复用 ZCode 桌面端已有的 GLM Coding Plan：

- **Z.AI OAuth**：`zcode login` 走 Z.AI 网页授权（对标 `codebuddy login` 的浏览器授权）。
- **bigmodel apiKey**：读取 `~/.zcode/v2/config.json` 中 `provider.builtin:bigmodel-coding-plan` 的配置（模型 GLM-5.3 / GLM-5.3-Flash，1M 上下文）。

> ⚠️ **凭证边界**：`~/.zcode/v2/config.json` 明文含 bigmodel / MiniMax 等 API key。本 skill 与网关代码均**不得**把这些 key 硬编码或复制进仓库；只运行时从 `~/.zcode/` 动态读取。真实配置与 `*.bak*` 不得入库、不得写入日志。

> ⚠️ **额度共享**：CLI 与 ZCode 桌面端共用同一 GLM Coding Plan 订阅额度池。「1.5 倍加成」是购买 plan 自带的加成，与入口数量无关——不会因多一个 CLI 入口就多一倍打折额度。CLI 消耗的积分从同一账户扣除。

> ⚠️ **headless 需先配 model provider——正确方式是 `/login`（实测 2026-08-27）**：
> 裸 `zcode --prompt "..."` 报 `Error: Model config is missing. Create /Users/maoking/.zcode/cli/config.json with an explicit model provider before running ZCode.`
> `~/.zcode/cli/config.json` 只有 `skills`/`plugins` 段，无 model provider。**逆向发现正确配置途径是 CLI 内置 `/login` 命令**，
> 支持 `zcode login` 交互选择（选项含 `BigModel Coding Plan API Key` 手动粘贴、`BigModel OAuth` 等待授权），
> 也会写入 `~/.zcode/cli/config.json` 的权威格式。**不建议手写该文件**——逆向 3 种结构均报 `ModelConfigMissing`，
> 因 CLI 的 model 配置与桌面端 `~/.zcode/v2/config.json` 是分离的两套存储。
> ⚠️ `login` 为交互式（涉及输入 api-key / OAuth），需用户手动执行，勿代输入。执行：
> ```bash
> zcode login   # 交互选 BigModel Coding Plan API Key，粘贴 coding plan api-key
> ```
> ⚠️ 该文件含敏感配置，改动前先备份；`*.bak*` 不得入库、不得写日志。

### 4.1 Provider 与账号参数全景（2026-08-27 逆向）

**两套分离的配置存储（关键事实，实测确认）**：

| 存储 | 路径 | 用途 |
|---|---|---|
| 桌面端 provider 注册表 | `~/.zcode/v2/config.json` | ZCode.app GUI 使用；provider 定义含**明文 apiKey** |
| CLI model 配置 | `~/.zcode/cli/config.json` | `zcode --prompt` / `app-server` 使用；由 `zcode login` 写入权威格式 |

CLI **不读** v2 注册表——桌面端已登录不代表 CLI 可用（实测手写 3 种结构均报 `ModelConfigMissing`）。
`~/.zcode/cli/config.json` 顶层只有 `skills`/`plugins` 段时即为未配置。

**v2 注册表的 provider 结构**（字段名，值脱敏）：

```json
"provider": {
  "<providerId>": {
    "name": "<显示名>",
    "kind": "anthropic",                     // 全部走 Anthropic 协议
    "options": { "apiKey": "<明文key>", "baseURL": "<端点>" },
    "enabled": true,
    "source": "custom",
    "models": { "GLM-5.3": { "reasoning": {...}, "limit": { "context": 1000000, "output": 128000 }, "modalities": {...} } }
  }
}
```

**本机已配置的 provider 清单**（2026-08-27 实测）：

| providerId | baseURL | 状态 |
|---|---|---|
| `builtin:bigmodel-coding-plan` | `https://open.bigmodel.cn/api/anthropic` | ✅ available（GLM-5.3 / GLM-5.3-Flash，1M ctx，输出 128K）|
| `builtin:bigmodel-start-plan` | `https://zcode.z.ai/api/v1/zcode-plan/anthropic` | ❌ `coding_plan_not_entitled` |
| `builtin:zai-coding-plan` / `builtin:zai-start-plan` | `api.z.ai/api/anthropic` / `zcode.z.ai/...` | ❌ `oauth_provider_inactive` |
| `builtin:bigmodel`（API Key 型） | `https://open.bigmodel.cn/api/anthropic` | 已配 key（按量付费型）|
| MiniMax（自定义 id） | `https://api.minimaxi.com/anthropic` | 已配 key（MiniMax-M3 / M2.7）|
| `CodeBuddy (本地)`（自定义） | `http://127.0.0.1:8787` | 用户自己的 codebuddy-gateway（deepseek-v4-flash）|

套餐可用性缓存在 `~/.zcode/v2/coding-plan-cache.json`（`entryStatus.items.<providerId>.status`，
`available` / `coding_plan_not_entitled` 等）。

**zcode-plan 专用通道**：`zcode.z.ai/api/v1/zcode-plan/anthropic` 是 ZCode 专属计费端点（路径名即
`zcode-plan`，配 JWT 型 apiKey）。当前账号对它 not entitled，实际走 bigmodel coding plan。

**CLI 内置 provider 常量**（引擎逆向）：`bigmodel` → `https://open.bigmodel.cn/api/anthropic`
（"BigModel Coding Plan"）、`zai` → z.ai 域。`/login` slash 命令变体：
`/login [zai-coding-plan|bigmodel-coding-plan|zai-coding-plan-api-key <key>|bigmodel-coding-plan-api-key <key>]`。

**`zcode login` 要粘贴的 key 从哪来**（运行时提取，不落盘、不入库）：

```bash
# 打印 bigmodel coding plan 的 apiKey（来源：桌面端 v2 注册表，同账号）
python3 -c "import json;print(json.load(open('$HOME/.zcode/v2/config.json'))['provider']['builtin:bigmodel-coding-plan']['options']['apiKey'])"
```

粘贴进 `zcode login` 交互（选 "BigModel Coding Plan API Key"）即可完成 CLI 配置。
桌面端与 CLI 共用同一 Coding Plan 订阅额度池，key 同源即同账户。

**折扣识别机制（引擎内部，静态逆向）**：`x-client-sig`（客户端签名）、`x-client-pow`（PoW）、
`x-off-peak-ticket-id`（错峰票）均由引擎自行计算（引擎日志脱敏清单含这些头名）；Coding Plan 计费
0.66x（≈1.5 倍额度）+ 非高峰 5 折 + 工作日 14:00–18:00 高峰 3 倍。

## 5. Skills 复用

ZCode CLI 原生扫描以下 skill 根目录（已实测，`zcode skills list` 输出确认）：

- `~/.zcode/skills/`（用户级）
- `~/.agents/skills/`（`user/agents` 来源，跨工具共享，含本机 21 个核心技能）
- 各插件 `skills/` 目录（`~/.zcode/cli/plugins/cache/zcode-plugins-official/*/skills/`）

> ⚠️ **2026-08-27 用户决策**：技能复用本次**先不做调整**（用户将另行处理）。本 skill 不自动软链/镜像技能目录。如需补充技能，由用户明确批准后在 `~/.zcode/skills/` 下软链，或开发 ZCode extension/plugin。

## 6. 已发现的坑点

### 6.1 `zcode` PATH 依赖桌面端（踩坑 A）

- 历史现象：`which zcode` → `not found`，PM 第一反应会以为没装。
- 现状：已软链到 `~/.local/bin/zcode`，`which zcode` 与 `zcode version` 均可用。
- 残余风险：软链指向 app bundle，卸载/升级桌面端或换机器后需重建软链；PM 每次应先 `zcode version` 探活，失败则检查 app 是否还在。

### 6.2 认证可能未就绪（踩坑 B）

- 现象：首次 `zcode --prompt` 报认证错误。
- 真相：CLI 的登录态可能与桌面端不完全共享，或需要先 `zcode login` 走一次 Z.AI OAuth。
- 处理：先 `zcode login` 完成浏览器授权（token 缓存后复用），再跑 headless。

### 6.3 折扣加成是否真实生效（待实测，踩坑 C）

- 现象：§1 所述 0.66x 折扣，静态逆向已在引擎中发现 `x-client-sig` / `x-client-pow` / `x-off-peak-ticket-id` 等身份头由引擎自行计算，理论上 CLI 天然吃满折扣。
- 待验证：通过 Coding Plan 用量/积分查询接口，对比「CLI 请求 vs 直接 API 请求」同量 token 的扣分比例，确认 0.66x 生效。这是本方案立身之本，未实测前不应宣称折扣已生效。

## 7. tmux Worker 启动示例

### 7.1 headless worker（多轮会话形态，可作 tmux worker）

> ⚠️ 独立 CLI **无交互 TUI**（见 §3.1）。worker 用 `--prompt` + `--resume`/`-c` 组出完整会话（见 §3.1 会话模型）。

```bash
# 单次任务（等价 codebuddy -p / claude -p）
zcode --prompt "任务描述"

# 多轮会话 worker（PM 可追问/纠偏，上下文保持）
zcode --prompt "开始任务..." --json          # 第 1 轮，拿 session id
zcode --prompt "调整方案..." --resume <id>   # 后续轮次
zcode -c --prompt "继续"                     # 或续最近会话

# 轮内流式监测（PM 实时看进度）
zcode --prompt "任务描述" --output-format stream-json

# 放进 tmux session（后台 + capture-pane 监测）
tmux new-session -d -s worker-zcode \
  'zcode --prompt "任务描述" --output-format stream-json'
```

### 7.2 通过 `app-server --stdio` 作 agent 子代理（非 Claude Code backend）

`app-server --stdio` 是 JSON-RPC over stdio 协议服务器（§3.3），**不是 Claude Code/Codex 可直接配置的
OpenAI/Anthropic HTTP backend**。正确用法是作为「agent 子代理」：用一个 stdio MCP 客户端包装它，
通过 `tools/call` + `session/send` 驱动。单独裸跑只会起一个等待 JSON-RPC 帧的服务：

```bash
# 裸跑（仅作为被驱动的服务，需外部客户端发 JSON-RPC 帧）
zcode app-server --stdio --surface terminal
```

**要把它接进 Claude Code/Codex，唯一可行路径是写一个适配器守护进程**（§3.3）：
起 `zcode app-server --stdio` 子进程 → `session/create`+`session/send`+`session/events` 驱动 →
把事件流翻译成 Claude Code/Codex 认识的协议。**但这改变语义**：是把 ZCode 当 agent 子代理，
不是当 model 上游——而且**折扣依赖 ZCode 引擎自算签名，适配器若重发请求到别处则吃不到折扣**。
工程代价高，默认不推荐；除非目标是"让网关驱动 ZCode 编码 Agent 做子代理"，那是另一类需求。

## 8. 适用场景

| 场景                                    | 推荐度 | 理由                                                      |
| --------------------------------------- | ------ | --------------------------------------------------------- |
| 复用 GLM Coding Plan 额度的独立 worker  | 高     | `--prompt` 独立可用，天然吃 0.66x 折扣                    |
| 吃满 1.5 倍额度加成                    | 高     | 走 ZCode 引擎请求路径，签名自算                            |
| 复用 `~/.agents/skills` 的 coding worker | 高     | 原生扫描，零配置                                          |
| 需要 ZCode 官方插件（browser-use 等）   | 中     | `plugins list` 显示 7 个官方插件可启停                     |
| 交互式 TUI 复用                        | ❌     | 独立 CLI 缺 `@zcode/tui`，TUI 仅桌面端 App 内可用         |
| 作为 Claude Code / Codex 的 HTTP backend | ❌     | 无 OpenAI/Anthropic HTTP 端点，仅 stdio JSON-RPC          |
| 作为 agent 子代理（适配器驱动）         | 低     | 可行但工程代价高、语义变为 agent 而非 model                |

## 9. 与现有 Skill 框架的集成建议

- **backend 标识**：`zcode`；profile 标识可写 `zcode-glm53` / `zcode-glm53-flash`。
- **spawn 集成**：若需接入 `spawn-worker.sh`，须先在 `check-dependencies.sh` 增加 zcode 的多源检测（先 PATH，再 fallback 到 `/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs`），并完成一次交互认证缓存 token。
- **权限模式**：ZCode 交互式 TUI 对标 Claude Code，无头 `--prompt` 是否支持 `-y` / `bypassPermissions` 需实测确认（未列入本文档默认假设）。
- **checkpoint 兼容**：ZCode 是否产生 `STATUS.json` 需实测；若产生则与 Claude Code 一致，否则需在 worker prompt 中要求自行写入 checkpoint 三件套。
- **额度监控**：暂无 CLI 查询剩余额度命令，需登录 ZCode 桌面端或查 Coding Plan 后台查看。

## 10. 相关文件与链接

- ZCode 引擎：`/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs`
- ZCode 配置：`~/.zcode/v2/config.json`（provider 定义）、`~/.zcode/skills/`、`~/.zcode/cli/`
- 用户级共享技能：`~/.agents/skills/`
- 本仓库 AutoClaw 上游设计（对比参考）：`docs/plans/2026-08-27-autoclaw-zai-upstream-design.md` + `docs/DECISIONS.md` DEC-013
- 网关架构（接入点参考）：`docs/ARCHITECTURE.md`

---

> **版本记录**：
>
> - 2026-08-27：初版，基于 ZCode.app 3.9.2 / zcode.cjs 0.16.5 实测编写。
> - 2026-08-27：实测补充——裸 `zcode` TUI 因缺 `@zcode/tui` runtime 报错（桌面端通过 SEA/Electron 注入，独立软链无此环境）；`--prompt`/`app-server`/`doctor`/`skills`/`plugins` 均不依赖 TUI。worker 深度逆向 `app-server` 协议（JSON-RPC over stdio，MCP+专有 session 混合，不可作 OpenAI/Anthropic HTTP backend，折扣依赖引擎自算签名）。
> - 2026-08-27（二）：逆向 `--prompt` 完整参数表确认会话能力——`--resume <id>`/`-c/--continue` 多轮续接 + `--output-format stream-json` 轮内流式监测，CLI 层即可组出完整 agent session（无需逆向 app-server 协议）。model provider 配置确认走 `zcode login` 交互（手写 config 三种结构均报 ModelConfigMissing，CLI 与桌面端 v2 是分离的两套配置存储）。新增 §4.1 Provider 与账号参数全景（两套存储、v2 结构、本机 provider 清单、zcode-plan 专用通道、login 取 key 命令、折扣识别头；apiKey 值一律不入库）。
> - 2026-09-07：§3.1"独立 CLI 不可用 TUI"结论修订——npm 存在社区**非官方**终端客户端 `zcode-app-cli`（kingsword09/zcode-cli，MIT，v3.11.2-21，`--version` 输出带 `zcode-app-cli` 前缀；实现=从 ZCode Desktop 提取官方 glm 内核直跑 + 自带 pi-tui 层，官方"无独立 CLI 分发"仍成立），上游 orca PR #16227/#16228 以该前缀为特征位将其识别为可交互 TUI 发行版（composer + bracketed paste）。本机软链指向的桌面捆绑版仍无 TUI，§3.1 对本机成立。发行版全景、上游 TUI 建模与 hook 能力详见公开版 `references/09-zcode-cli-worker.md` §11。
