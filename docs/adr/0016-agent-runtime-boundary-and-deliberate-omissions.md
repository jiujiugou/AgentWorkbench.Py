# 0016 Agent 运行时的能力边界与主动放弃清单

- **状态**：Accepted（未实施）
- **日期**：2026-11-06
- **实施仓库**：`../AgentWorkbench`（C# 主服务）
- **配套**：[0013](0013-csharp-host-python-rag-sidecar.md) 谁跑、[0014](0014-runtime-is-maf-with-bounded-scope.md) 用什么跑、[0015](0015-feature-domains-under-host-split.md) 各放弃/加强什么
- **本文回答**：一个 agent 运行时**必须**有什么；C# 版现在实际有什么；**主动放弃**什么（每条带理由与重开条件）

---

## 一、运行时是什么

一句话判据：

> **把「模型想做什么」变成「系统真的做了什么」，并且在每一步都能说不、都能留下证据。**

它不认识采购单、供应商、库存、金额阈值——那些是**应用层**（ADR 0015 的 F8/F9）。运行时只认识：工具名、参数、结果、迭代轮次、终态、轨迹。

**边界一句话**：运行时负责「这次调用该不该发生、发生了什么」，**不负责「这个业务动作对不对」**。越界往 businessman 那边写，运行时会变成一团谁都不敢改的业务泥；往模型那边退，就成了「把 prompt 拼好发出去」，那不是运行时。

这与 C# 版三个模块 README 的自述完全一致：`AgentHarness` 只装配、`AgentLoop` 只管「停」、`ToolExecution` 只管「调与记」。

---

## 二、五条不变量（一个运行时必须成立的）

| | 不变量 | 反例（写不出反例的判据不要写） | C# 现状 |
|---|---|---|---|
| **R1 可停** | 任何 run 都能在预算内被终止，且**终止原因可区分**：跑满 ≠ 答完 ≠ 循环 ≠ 超时 ≠ 取消 | 跑满 6 轮被框架逼出一段文本 → 若记成 `succeeded`，就是「被截断却当成答完」 | **已成立**。`InspectCompletion` 专门区分；10 种终态×归类码 |
| **R2 可拦** | 工具调用**发出前**过白名单 + 参数校验 + 权限；结果**回填前**可被否决 | 守卫若在调用后才判，重复建单已经发生；`Ask` 无人应答却放行，就是审批失效 | **半成立**。调用前守卫、调用后 `IToolResultVeto`、fail-closed 审批都在；**缺业务参数校验与按人判定** |
| **R3 可回放** | 意图、参数、结果、失败原因、终态全部落库，且**没有绕过记录的路径** | 挂了 `AIContextProviders` → 框架在外层再包一圈函数环，守卫与轨迹全被绕过、全部退化成 `succeeded` | **实现在、防线失效**（2026-11-07 实测更正）：`ExecutionTrace` / `ExecutionTraceStore` 在，但回归闸所指的 `tests/AgentWorkbench.Evaluation.AgentTask` 工程已删，`ArchitectureBudgets.Trace_replay_must_be_consumed_by_evaluation_tests` 因此变红（架构测试 12/13）。见 §四·附 |
| **R4 可恢复** | 中断后能从明确断点续跑，**且不重复执行外部副作用** | 超时后盲目重放 → ERP 侧两张采购单 | **不成立**。README 第一条缺口：Harness run 无 resume、无断点快照 |
| **R5 不自作主张** | 运行时不替模型决定业务，也不替业务决定模型；不暗中拼装上下文 | harness 暗做检索注入 → 模型拿到的证据与轨迹里的不一致 | **已成立**（`AgentWorkbench.Context` 已砍）；但要守住，见 §五 A1 |

> R1 / R5 已做对；R3 的**实现**做对了、但防退化的回归闸当前失效（见 §四·附）。
> 这三处恰恰是最难补、最容易在重写时丢掉的部分，价值常被低估。

---

## 三、C# 版运行时现在实际有什么（2026-11-06 实测）

| 能力组 | 具体 |
|---|---|
| **循环本体** | MAF 的 `ChatClientAgent`；函数环本体是 `Microsoft.Extensions.AI` 的 `FunctionInvokingChatClient`（由 `ToolExecution` 装配，落到 `MaximumIterationsPerRequest`）。Harness **不自己实现循环** |
| **执行控制** | `MaxIterations=6`、`TimeoutSeconds=30`、`MaxRepeatedToolCalls=3`、`MaxTokensPerRun`；`AgentRunGuard` 在调用前查重复指纹与用量、`InspectCompletion` 在收尾查「是否被截断」；超时抛 `AgentRunTimeoutException` |
| **工具管辖** | `ToolCatalog` + 白名单 `IOptionsMonitor`（热重载）、调用前守卫、调用后 `IToolResultVeto`、参数按名绑定、`ToolCallTrace(Iteration/Name/Arguments/Result/Error)` |
| **审批** | `Auto / Ask / Deny` 三态、**fail-closed**（超时/断连/无审批者一律拒绝）、`ApprovalTimeoutSeconds=120`、三个拒绝归类码（`policy_denied` / `user_denied` / `unavailable`） |
| **终态与收口** | 10 种 status×code；`RunAsync` 与 `StreamAsync` 共用 `PrepareRun` 与 `FinishSucceeded`，**同一 loop、同一守卫、同一账本**；消费者提前退出仍等生产侧收尾 |
| **会话** | `SessionStore` / `SqliteSessionStore` / `SessionStateRepository`（`Memory` + `Memory.Persistence`）、`ChatHistoryProvider`、`AgentSessionFactory` |
| **上下文** | **没有专门模块**。原 `AgentWorkbench.Context` 项目在阶段 4 已删，只剩 `ContextOptions`（一个能力位，供 `GetCapabilitiesAsync` 读取）。碎片散在三处：指令走 `RunInstructionsChatClient` 装饰器、历史走 `ChatHistoryProvider`、证据靠模型主动调 `search_knowledge` 工具 |
| **工作流** | MAF Workflows；`workflow_steps.succeeded` 作检查点放行、`ResumeAsync`、`RerunStepAsync`、步骤级重试/超时、提示词版本化、后台单读者队列 |
| **长期记忆** | `MemoryItemRepository`、`MemoryRetentionService`、`remember/recall/forget`、scope 隔离、保留期清理 |
| **观测** | `ExecutionTrace` / `ExecutionTraceStore`、`HarnessStreamEvent`、`ToolCallTrace` 桥接 |

### 谁做「模型循环 / 工具调用 / 上下文 / 会话 / 任务执行」

`HarnessServiceCollectionExtensions.cs` 的注释原文：把上述能力「装配成一次可运行的 Agent 任务，**自身不实现任何一项能力**」；`AddNitroAgentHarness` 只注册 `ContextOptions` 与 `IAgentHarness` 单例，其余由各模块的 `AddNitro*` 自行注册，**本方法不负责装配它们**。所以这五项**没有一项是 Harness 实现的**：

| 能力 | 实际承担者 | Harness 做什么 |
|---|---|---|
| 模型循环 | MAF `ChatClientAgent` + `Microsoft.Extensions.AI.FunctionInvokingChatClient`（由 `ToolExecution` 装配） | `BuildAgent(agentClient)`；不写循环 |
| 工具调用 | `ToolExecution`（白名单、执行管道、守卫、审批、轨迹） | `_pipeline.Create(...)`，并把 `Approver` 透传下去 |
| 上下文 | **无专门模块**（见上表）；指令靠装饰器、历史靠 `ChatHistoryProvider`、证据靠模型调工具 | 用户原文**直接进模型**，不暗中拼装 |
| 会话 | `Memory` + `Memory.Persistence` | `GetOrCreateAsync(sessionId, factory)` 与收尾 `SaveAsync` |
| 任务执行 | 一次 run = Harness 的 `RunAsync`/`StreamAsync`；长任务 = `Workflow`（队列 + Worker + 检查点） | 前者是它唯一的执行粒度；后者不排队、不调度 |
| 何时停 | `AgentLoop` | `_loop.CreateGuard(...)`，只把守卫装起来 |

**Harness 真正自己做的只有三件**：`PrepareRunAsync` 接线、两条出口共用一套账本、`FinishSucceededAsync` 把结局归类并落轨迹。**「归 C#」不等于「由 Harness 实现」**——这是最容易混的一层。

**结论**：运行时的**主体不需要重写**。真正要做的是 §四 的补齐、§五 的放弃，以及 ADR 0014 要求的「把渗进业务层的框架类型收回去」。

---

## 四、必须补的 7 项（对应 R2 / R4 与跨进程）

> **§四·附 · 另有一条必修，不列入 7 项**（它不是补能力，是补防线）：补回禁用 `AIContextProviders` 的回归闸。
> 2026-11-07 实测该闸所指评测工程已删、`ArchitectureBudgets` 因此变红（架构测试 12/13）。
> 证据与处置见 `docs/架构/CSharp底座现状.md` §六第 1 条、§八。

| # | 补什么 | 现在 | 不补会怎样 |
|---|---|---|---|
| 1 | **HITL 挂起 → 人批 → 恢复** | 审批是**同步 `Ask`**，占住连接等人，`request_info` / `Checkpoint` 零命中 | 跨天审批不可能；人不在就白烧一轮预算 |
| 2 | **Harness run 层 checkpoint** | Workflow 层有检查点，**Harness 层没有**——两层别混 | R4 不成立；中断只能整条重跑 |
| 3 | **幂等键 + 去重表** | `Idempot` 零命中 | 超时后无法判断 ERP 侧是否已执行（F9 REQ-SAFE-01） |
| 4 | **业务参数校验层** | 只有「参数类型绑定」，没有金额上限 / 枚举 / 跨字段一致性 | R2 的半边缺口；`Validate` 零命中 |
| 5 | **权限判定前移到调用前** | 只有白名单（按工具名），没有「这个人能不能对这个对象做这个动作」 | PDP 唯一性落不到工具上 |
| 6 | **审计事件表 + 五字段错误信封 + `request_id`** | `AuditEvent` 零命中；错误只是 `new { code, message }` | 跨三跳之后查不回来 |
| 7 | **Step 加 `IdempotencyKey` / `ApprovalId` / `ExternalRef`** | `AgentStepRecord` 有 `Tool/Arguments/Result/Error`，缺这三列 | 「这次调用对应哪张 ERP 单据」没有落点 |

> **§四·附二 · 2026-11-07 按 ADR 0018 重判**：7 项数量不变，但性质分两档。
> 第 1 项（HITL 挂起恢复）、第 2 项（Harness 层 checkpoint）、第 6 项里的 middleware 挂点
> 属于「框架已给机制」→ 改为**启用 MAF 对应能力**，自己只写业务语义与存储后端；
> 第 3/4/5/7 项与审计事件表框架不给，仍自研。
> 逐项证据与三档判据见 `docs/adr/0018-standard-mechanisms-must-use-framework.md` §二／§三。

> 顺序：1→2→3 是 R4；4→5 是 R2；6→7 是追责。ADR 0015 §七 的六步实施顺序从表结构开始，与本表不冲突——那三步先做 `UserRecord`/`KnowledgeBase`/`message_citations`。

---

## 五、主动放弃清单

分三类。**每一条都写理由与重开条件**：「以后再说」不是决定，能挡住以后重提的才算。

### A. 沿袭这个库已有的放弃（继续放弃，不复活）

| 放弃 | 依据 | 重开条件 |
|---|---|---|
| **A1 harness 暗做上下文注入/检索** | `AgentWorkbench.Context` 整个项目在阶段 4 已砍：检索改由模型主动调 `search_knowledge`，用户原文直接进模型，harness 不暗中拼装。这是 R5 | 若出现「判定与模型必须拿同一份证据」的强约束，由 **C# 侧显式工具调用**实现，不是恢复 ContextBuilder |
| **A2 结构化输出** `IStructuredChatService` | 阶段 4 已砍。MAF 的 function tools + JSON Schema 已覆盖参数结构化 | 不重开；若需要输出结构化答案，走 `ActionResult`，不走独立服务 |
| **A3 原生客户端出口** `IChatClientProvider` | 阶段 4 已砍。模型接入统一在 `Microsoft.Extensions.AI` 的 `IChatClient` | 不重开 |
| **A4 自研循环** | 三个 README 反复写「底座不重写一遍」 | 不重开。MAF 若停更，走 ADR 0014 的适配层迁移，不重写循环 |
| **A5 长期记忆自动召回** | Memory README 明写「只在模型主动调 `recall` 时生效」，阶段 3 的自动召回至今未做 | 有真实使用数据、能定义「什么算记错」时再评估；ERP 里记错会**持续犯错** |

### B. ERP 场景新增的主动放弃（本次决定）

| 放弃 | 理由 | 重开条件 |
|---|---|---|
| **B1 多 agent / 自主规划 / 任务分解** | MAF 有 sequential / handoff / group-chat / Magentic，全部不用。ERP 要确定性：谁决定下一步、迭代几次、调哪个工具都必须可追溯。两个 agent 争调度权，R1/R3 立刻破 | 出现**确实需要不同角色协作**且动作彼此独立、可分别追责的流程（第二个 ERP、第二类动作之后） |
| **B2 自动重放带副作用的工具调用** | 跨系统超时后状态未知，重试可能重复建单。**超时不重试，只查证**（F9 REQ-SAFE-03）。**不放弃** `ModelRuntime` 对模型请求的重试——那一层没有外部副作用；放弃的是**工具调用/ERP 写动作被自动重放** | 目标 ERP 提供幂等接口且能按外部单号查状态时，由 C# 显式发起状态查询；**永远不自动重放写动作** |
| **B3 并行工具调用** | 一次 run 只做一件事。并行会让审批、幂等键、轨迹顺序全部失去确定次序 | 只读批量查询且能证明无副作用时，可放开**只读**并行；写动作永不并行 |
| **B4 模型自行计算** | `CalculatorTool` / `GetCurrentTimeTool` 删除。数字（金额、汇率、税额、库存）一律来自工具或 ERP 表；调研里最典型的事故是「合法推理出错误汇率」 | 不重开 |
| **B5 运行时动态发现/装载工具** | 白名单热重载保留，**但不允许模型发现或注册新工具**。工具集是编译期 + 配置期确定的，可审计 | 不重开（平台化才会需要，见 C） |
| **B6 自动执行写动作** | 写动作默认 `Ask`，必须挂起→人批→执行。无人可问即 fail-closed | 只在**只读**动作上放开 Auto；某个写动作要放开，须单独立一条 REQ 并写清金额/品类/时间窗边界 |
| **B7 长期记忆（第一版）** | A5 的加强版：`remember/recall/forget` 与 `SqliteLongTermMemoryStore` **停用不删** | 同 A5 |
| **B8 智能上下文压缩 / 自动摘要** | 用**固定窗口 + 显式截断**，不搞「聪明」的压缩。压缩会改写历史，而 R3 要求轨迹与当时输入一致 | 会话确实长到影响成本，且能证明截断不影响可回放性时 |

### C. 平台化能力（明确不做，不设近期重开）

| 放弃 | 理由 |
|---|---|
| **C1 可视化编排编辑器 / 插件市场 / agent 市场** | 没有第一个真实使用者的情况下，这三样只会生产「可被自己验收的东西」。Python 版 113 条判据 vs 2 条会话就是代价 |
| **C2 按租户隔离 agent 实例 / 动态人格配置** | 权限必须是唯一 PDP，人格不是权限。第一版团队隔离足够 |
| **C3 通用对话 / 闲聊入口** | 见 ADR 0015 §6.3：F7 停用。本产品不变量是「动作可追责」 |

---

## 六、README 自己列的缺口：哪些修、哪些主动不修

`AgentHarness/README.md` §二 与 `ToolExecution/README.md` §二 已列缺口。逐条给处置，避免「以后再定」：

| 已知缺口 | 处置 | 理由 |
|---|---|---|
| run 不能恢复、无断点快照 | **必须修** | R4，§四 第 1、2 项 |
| 会话重启即丢（内存版） | **必须修** | 状态全在 C#（ADR 0013），装 `AddNitroMemoryPersistence` 即可 |
| 同一 `sessionId` 并发 run 会串消息 | **主动不修并发，改为「同会话串行」** | 用约束代替实现：B3 已放弃并行。串行比加锁+排队简单，且符合「一次一件事」 |
| 历史无限长无裁剪 | **主动不做压缩，改为固定窗口 + 显式截断** | B8。裁剪可观测、可预期；压缩会改写历史 |
| 执行记录不能按条件查 | **必须修** | F9 REQ-SAFE-04 要「任取一笔执行能追」，只有 `Save/Get` 不够 |
| 对话与轨迹不关联（`agent_runs` 无 `conversation_id`） | **必须修** | 从一次聊天跳不到它的 run，追责断链 |
| 工具重名静默保留第一个 | **必须修，改为启动即失败** | 静默挑一个是 fail-open；R2 要求 fail-closed |
| 参数留痕丢类型（`{"failTimes":"0"}`） | **必须修，存原始 JSON 文本** | 审计要能还原当时的真实参数，文本化会丢类型与嵌套 |

> 「主动不修」只有两条（并发、压缩），其余六条都是必须修。**放弃不等于容忍缺陷**：放弃的是「做得更聪明」，不是「把已知的错留着」。

---

## 七、运行时与应用的边界（防止越界的具体判据）

`AgentHarness` / `AgentLoop` / `ToolExecution` 三个工程里**不得定义、不得判定**这些业务概念：`PurchaseOrder`、`Supplier`、`AmountLimit`、`ApproverRole`，以及 `IdempotencyKey` 的生成规则和 `ExternalRef` 的业务含义。

它们可以出现、也只该出现的是：`ToolName`、`Arguments`、`Guard`、`Veto`、`Status`、`RunCode`、`Trace`、`SessionId`，以及**透传** `IdempotencyKey` / `ApprovalId` / `ExternalRef` 三个字段——携带不等于理解。

**幂等键的边界最容易破**：键必须由 C# 用 `request_id` 生成后下传（ADR 0013），运行时只负责把它记进 Step，**不负责决定它的值**。运行时若开始生成业务键，就等于有了第二个状态源。

---

## 八、第一批判据（5 条，全部能写出反例）

| # | 判据 | 反例 |
|---|---|---|
| **RR-1** | 任一 run 的终止都能归因到唯一一个 RunCode，且「跑满被逼收口」与「模型自己收口」可区分 | 预算 2、模型还想再调 → 必须记 `iteration_limit`，不是 `succeeded` |
| **RR-2** | 任何工具调用被拒（策略/人/超时/白名单）都未产生外部调用，且轨迹里有对应拒绝码 | `Ask` 超时 → ERP 侧无新单据，轨迹有 `approval_unavailable` |
| **RR-3** | 同一 `request_id` 重复提交，外部副作用只发生一次 | 连打两次建单 → ERP 侧一张单 |
| **RR-4** | 中断后恢复，不重放已完成的写动作 | 杀掉进程再恢复 → 不产生第二张单 |
| **RR-5** | 从任一次 run 能查出：谁发起、调了什么、参数原文、谁批的、外部单号、终态 | 抽一笔执行 → 六项齐全 |

**只有 5 条。** §五 放弃了 18 项能力，不是为了省事，是为了让这 5 条能被真正验证。Python 版的教训是「验证做得太早、太多、对象还没确定」。

---

## 九、后果

- **不是造运行时**：C# 版运行时主体已在，R1/R3/R5 成立。本次工作 = 补 7 项 + 修 6 个已知缺口 + 隔离框架类型（ADR 0014 §六）。
- **放弃清单是硬约束**：B1~B8、C1~C3 每一条都有重开条件。以后有人提议「加个多 agent 协作」，先回答触发了哪条重开条件，答不上就不做。
- **两层 checkpoint 必须分清**：Workflow 层的「步骤 succeeded 放行」与 Harness 层的「run 断点续跑」是两件事，混成一个会出「框架说继续、业务说已完成」的分裂。
- **`ToolExecution/README.md` 里那句「重名静默保留第一个」是 fail-open**，与本 ADR R2 冲突，实施时一并改掉。

## 十、重开条件总表

| 组 | 何时重新讨论 |
|---|---|
| B1 多 agent | 第二个 ERP / 第二类动作跑通，且出现确实需要角色协作、可分别追责的流程 |
| B3 并行 | 只读批量查询且证明无副作用；写动作永不 |
| B6 自动执行写动作 | 某个写动作单独立 REQ，写清金额/品类/时间窗/升级触发器 |
| B7/B8 记忆与压缩 | 有真实使用数据，能定义「什么算记错」「压缩不破坏可回放」 |
| C 平台化 | 有第一个真实使用者，且第一个 ERP 的动作已稳定运行 |
