# ADR 0014 · Agent 运行时用 Microsoft Agent Framework，限定三块能力

- **状态**：Accepted（**框架已在 C# 版使用**；本文限定使用面并列出补齐项）｜ **日期**：2026-11-06
- **覆盖范围**：C# 主服务（`../AgentWorkbench`）。与 ADR 0013 配套——0013 定「谁来跑」，本文定「用什么跑、还差什么」。
- **机制**：本文 §2 使用面、§3 不依赖框架类型的边界、§6 改动清单。判据写在 §1。

## 背景

**2026-11-06 复核更正**：C# 版**早已引入并使用 MAF**，不是「要不要引入框架」的问题。
此前几轮只按工程目录判断，漏看了 `.csproj` 与 `using`，据此写下的「先别引入」「框架是新增风险」不成立，
本节按实测重写。

`Microsoft.Agents.AI` / `Microsoft.Agents.AI.Workflows` 均锁 **1.23.0**，目标框架 `net10.0`。
MAF 2026-04 已 1.0 GA，.NET + Python 双语言，官方承诺长期支持；
官方文档把 workflows 能力分成 Composition / Interaction & durability（**Human-in-the-loop**、**Checkpoints and resuming**）/
Operations（**Observability**：spans、metrics、events）/ Multi-agent orchestration 四组。

### 现状核对（2026-11-06 实测 `../AgentWorkbench/src/`）

| 已用 | 具体位置 |
|---|---|
| `ChatClientAgent` / `ChatClientAgentRunOptions` | `AgentHarness/AgentHarnessService.cs`、`Conversation/ChatService.cs` |
| `AIFunctionFactory.Create` 工具 | `SearchKnowledgeTool`、`ListDocumentsTool`、`RecallTool`、`RememberTool`、`CalculatorTool`、`GetCurrentTimeTool` |
| `WorkflowBuilder` / 执行节点 | `Workflow/Execution/WorkflowGraphBuilder.cs`、`WorkflowStepExecutorNode.cs` |
| `AIAgent` + 会话与长期记忆持久化 | `Memory/AgentSessionFactory.cs`、`Memory.Persistence/SqliteSessionStore.cs`、`SessionStateRepository.cs`、`SqliteLongTermMemoryStore.cs` |
| 模型接入 | `Microsoft.Extensions.AI.OpenAI` 10.10.1、`OllamaSharp` 5.5.0 |
| 审批（**自研**） | `ToolExecution/ToolApproval.cs`、`Safety/ToolApprovalPolicy.cs`、`ToolExecutionPipeline.cs` 等 7 个文件 |
| 轨迹（**自研**） | `Observability/ExecutionTrace.cs`、`ExecutionTraceStore.cs` |

| **零命中（要补的）** | 说明 |
|---|---|
| `request_info` / Human-in-the-loop | 审批目前是自研同步策略，没有框架级挂起与恢复 |
| `Checkpoint` / `CheckpointManager` | 有 session 存储，没有**工作流进度**保存 |
| `Middleware` | 权限、审计、幂等散在各处，没有统一挂点 |
| MCP / A2A | 工具协议未接；按 §2 不扩大使用面，第一版可暂不做 |

**所以要做的不是「引入框架」，而是：补齐三个零命中 + 把已经渗进业务层的框架类型收回适配层**（§3 目前被违反）。

## 一、决定

**C# 主服务继续采用 MAF（`Microsoft.Agents.AI` + `Microsoft.Agents.AI.Workflows`），
把使用面限定在三块能力，补齐 HITL / checkpoint / middleware，明确不使用多 agent 编排。**

判据：ERP 场景要的是**确定性、可审批、可恢复**，不是自主性。凡是增加「模型自己决定下一步」空间的框架能力，在这里都是负资产。

## 二、使用面（写死，不扩大）

| 用 | 用途 | 对应我们要解决的问题 |
|---|---|---|
| **Function tools** | 工具定义与调用协议 | ERP 动作作为 `AIFunction` 暴露，参数走 JSON Schema |
| **Workflows + Checkpoints + Human-in-the-loop** | 图式编排、进度保存与恢复、暂停等外部输入 | 审批挂起 → 人批 → 恢复执行；跨天流程中断后续跑 |
| **Middleware + Observability** | 调用前后统一挂点；spans / metrics / events | 权限拦截、审计开记、重试策略、耗时与失败归因 |

**不用**（这一栏和上一栏同等重要）：

- **Multi-agent orchestration**：sequential / concurrent / handoff / group-chat / Magentic 一个都不用。
- **不让 MAF 持有业务状态**：单据引用、审批记录、幂等键、审计事件仍在自己的库里。
  MAF 的 checkpoint 只恢复**框架内部执行进度**，不保证 ERP 侧没被执行两次。
- **不把 MAF 的 observability 当唯一审计**。审计要落自己的表，框架 trace 只做补充。
- **不用 MAF 的 agent 抽象承载权限判定**。PDP 仍是 `judge_*` 那一套，MAF 只负责调用发生。

## 二·附：九项运行时能力的逐项归属

| 能力 | MAF 提供 | 自己写 | 一句话提醒 |
|---|---|---|---|
| **模型循环** | agent 内部的 function-calling 循环、终止条件、循环检测 | 迭代上限 / 预算 / 超时的业务阈值；每轮落 step | `AgentRunGuard` 已有同类概念，对齐即可 |
| **工具调用** | function tools 的定义与调用协议、middleware 拦截点 | 工具白名单、参数校验、权限判定、幂等键 | 白名单与参数校验**不能交给模型** |
| **上下文** | `AgentSession` + context provider 接口 | 装填规则：放哪些证据、编号、字符预算 | 难的不是「放进去」，是「放什么进去」 |
| **会话** | `AgentSession` 对象，可序列化恢复 | 会话表、归属判定、引用快照 | 会话对象 ≠ 会话历史 |
| **任务执行** | workflow executors 与调度 | 任务定义、业务状态机、业务终态 | 业务终态与框架终态要能对上 |
| **会话历史** | **不提供持久化** | 落库、查询、权限、快照、归档 | 归 C# 业务库（ADR 0013 §二） |
| **任务状态** | workflow 运行状态 | 业务状态：这张单据办到哪一步 | 与 Checkpoint 不是一回事 |
| **长期记忆** | context provider 接口 | 存储、写什么、何时写、如何衰减、如何防污染 | **第一版不做**，理由见下 |
| **Checkpoint** | workflows 的 checkpoints / resuming + 序列化 | 存储后端、存档粒度、恢复后的一致性校验 | 只在挂起点与写动作前存 |

三条必须分清的界线：

1. **会话 ≠ 会话历史。** `AgentSession` 是运行时对象，进程重启要靠序列化；「历史」是查询问题——谁能看、看多久、
   当时的引用快照还在不在。后者只能落自己的表。C# 版已有 `SqliteSessionStore` / `SessionStateRepository`，可直接扩展。
2. **任务状态 ≠ Checkpoint。** 任务状态是业务视角（采购单待审批），Checkpoint 是执行视角（工作流停在第 4 个节点）。
   混成一个字段，恢复时会出现「框架说继续、业务说已完成」的分裂。
3. **长期记忆第一版不做。** C# 版已有 `SqliteLongTermMemoryStore` + `RecallTool` / `RememberTool`，但框架不给
   「记什么、记多久、怎么防污染」。ERP 里记错的代价是**持续犯错**。第一版先停用不删，
   等有真实使用数据、能定义「什么算记错」时再开。

## 三、最关键的一条：领域层不依赖框架类型

**自己定义动作的类型，在适配层转成 MAF 的类型。目前这条被违反。**

```
ErpAction / ActionSpec / ActionResult / ActionOutcome     ← 自己的领域层，不 import MAF
        │  适配层（唯一 import MAF 的地方）
        ▼
AIFunction + JSON Schema
        │  套 middleware
        ▼
权限 PDP → 参数校验 → 幂等检查 → 审计开记 → 执行 → 审计收口
        │
        ▼
MAF Workflow（单 agent + 工具节点 + 审批挂起节点 + checkpoint）
        │
        ▼
自己的库：run / step / 审批 / 幂等键 / 审计事件
```

实测的违规点（§6 第 1 步要处理）：

- `AgentHarnessService.cs` 直接 `new ChatClientAgent(...)` / `ChatClientAgentRunOptions`
- `Conversation/ChatService.cs` 直接 `new ChatClientAgent(...)`
- `Memory/AgentSessionFactory.cs` 构造函数吃 `AIAgent`

三条收益，都是可验证的：单测不依赖框架；框架可替换；业务语义不被版本绑架。
**违反这条的写法一律不合并**：`ErpAction` 里出现 `AIFunction`、`ChatMessage`、`AgentSession` 等框架类型。

> **归属澄清（ADR 0017）**：`ErpAction` 是**适配器侧的动作定义**，不是底座类型；底座侧统一用 `ActionSpec`。
> 上面把二者并列叫「自己的领域层」，只是说「都不 import MAF」，**不表示 `ErpAction` 属于运行时底座**。
> 底座五层不得出现业务概念，见 ADR 0017 §二。

## 四、模型接入

C# 版已用 `Microsoft.Extensions.AI.OpenAI` 10.10.1 + `OllamaSharp` 5.5.0，
抽象层是 `Microsoft.Extensions.AI` 的 `IChatClient`——**这是唯一允许替换模型的位置**。
把 `base_url` 指向现有智谱端点即可，**不需要新增包**。

两个前提必须实测，不通过就换模型而不是改上层：

- `glm-4-flash` 的 tool calling 是否稳定（MAF 文档明确「不是所有模型都支持 function calling」）。
- 参数是否会出现「结构对但值错」——这是 ERP 场景最危险的形态，比不调用更糟。

## 五、版本策略

- 当前锁 `Microsoft.Agents.AI` / `.Workflows` **1.23.0**；.NET 侧 2026-10 已有更高版本。
  **升级不是默认动作**，只在有明确理由时做（安全修复、需要某个能力）。
- 每次升级必须跑一遍**动作回归清单**（见 §6），并确认三处是否破坏性变更：
  工具调用协议、checkpoint 序列化格式、middleware 签名。
- 不用浮动版本号。私有化部署不能让生产跟着周更走。

## 六、改动清单（按依赖顺序）

1. **先做领域层隔离**（§3 当前被违反）：把 `AgentHarnessService.cs`、`ChatService.cs`、`AgentSessionFactory.cs`
   里直接出现的 `ChatClientAgent` / `AIAgent` 收进一个新的适配工程；定义 `ErpAction` / `ActionSpec` / `ActionResult`。
   **这一步不改框架，只把框架类型从业务层挪出去。**
2. **自研审批对齐到 MAF HITL**：`ToolApproval` / `ToolApprovalPolicy` 的业务规则保留，
   执行方式换成挂起 → 人批 → 恢复（`request_info` + response handler）。审批记录落自己的库。
3. **加 checkpoint 存储后端**：复用 `SqliteSessionStore` 的持久化模式，验证「进程重启后能续跑」。
4. **加 middleware 管道**：权限 PDP → 参数校验 → 幂等检查 → 审计开记，替换散在各处的调用。
5. **工具换血**：`CalculatorTool` / `GetCurrentTimeTool` 换成 ERP 只读动作；
   `SearchKnowledgeTool` / `ListDocumentsTool` 改为调 Python 侧 `/retrieve`；
   `RecallTool` / `RememberTool` 按 §二·附先停用不删。
6. **业务侧补齐**：五字段错误信封 + `request_id`、幂等/去重表、审计事件表、
   权限维度扩展为 `业务对象 × 操作 × 数据范围 × 金额阈值`。
7. **动作回归清单**：查询、写入、重复提交、超时、审批拒绝、审批超时各一条。

## 七、否掉的候选，以及 Semantic Kernel 的真实位置

| 候选 | 结论与理由 |
|---|---|
| **自研运行时** | 否。C# 版现在就是「MAF + 自研策略层」，再退回去重写循环没有收益；ADR 0013 的目标是「不再堆自证工程」 |
| **Semantic Kernel 作为编排层** | 否——但**不是「SK 被淘汰」**。SK 仍在发版（.NET 1.8x / Python 1.4x，2026-10），定位下沉为基础层：kernel、plugin、connector、RAG / embedding、filter。新 agentic 能力（checkpoint、HITL、多 agent、声明式工作流）只进 MAF；且第三方实测指出 SK 各家 process API 在 checkpoint / HITL 上「不一致」 |
| **LangGraph** | 否。它是 Python，与 ADR 0013「编排在 C#」直接冲突 |

**SK 不是被废掉，是被下沉。** 实测：`Microsoft.SemanticKernel.Core` 1.81.0 已在 `AgentWorkbench.Documents` 里，
但只用于 `Chunker.cs` 的 `Microsoft.SemanticKernel.Text` **文本切块**——正是「下沉成工具类」的实例，不构成编排层。
MAF 建在 SK 与 `Microsoft.Extensions.AI` 之上，原生接受 `[KernelFunction]` 插件，两者可共存。

## 八、后果

**换来的**：不需要重写运行时——agent、工具、workflow、session 持久化都已在；
要补的是 HITL、checkpoint、middleware 三个点，以及把框架类型收回适配层。

**代价（不粉饰）**：

- MAF 的 agent 抽象比我们需要的重，只用三块意味着要主动约束，否则会不知不觉用上多 agent。
- **升级节奏是真实风险**：锁的是 1.23.0，框架周更，SK 已跑三年、MAF 1.0 才半年多。
  对策是 §3 隔离 + 锁版本 + 回归清单；最坏情况迁回自研时只改适配层。
- 出问题时多一层「是框架还是自己」的排查成本。§3 的隔离能让大部分业务问题与框架无关，
  但运行时问题仍要读框架源码——**绕过一次，以后每次升级都要重新发现一次**。

## 九、重开条件

- MAF 停止维护、许可变更、或 .NET 侧长期落后于 Python 侧 → 因 §3 隔离，迁移成本可控，届时重议。
- 出现**真正需要多 agent 协作**的场景（不是单 agent 调多个工具，而是多个 agent 各持不同权限互相交接）→ 重议 §2。
- GLM 的 tool calling 无法通过换模型或换 `IChatClient` 实现解决 → 运行时上层重议，与选 MAF 无关。
