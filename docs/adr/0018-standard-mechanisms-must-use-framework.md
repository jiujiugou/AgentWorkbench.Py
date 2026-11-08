# ADR 0018 · 标准机制一律用框架，自研只允许在业务语义层

- **状态**：Accepted（判据）／**未实施**
- **日期**：2026-11-07
- **来源**：用户 2026-11-07 11:14 指令「标准部分自己不实现，可以使用框架就绝对不自己做」
- **与既有 ADR 的关系**：本篇是**优先级判据**，管「机制由谁实现」；不改 ADR 0013（进程与状态）、0014（运行时选型）、0016（运行时边界）、0017（业务语义归属）的任何归属决定。冲突时由本篇裁定实现方。

---

## 一、判据：不是新标准，是已有标准的提升

`pyproject.toml` 2026-11-04 已经立过同一条：

> 判据只有一条：**这件事在行业里有没有标准形态。**
> 有标准形态 → 用库。……没标准形态 → 不假装用库。本项目的授权与隔离不在任何库的视野里。

`tests/test_gates.py::test_component_table_matches_dependencies` 用四条规则强制它：`adopted` 必须装、装了必须登记、`rejected` 不许 import、拒绝必须写重开条件。

本次提升两点，这才是新内容：

1. **从「包」提升到「机制」**：原规则只管有没有引入第三方包，管不到「框架已经提供这个能力、我们却自己写了一遍等价物」。
2. **从 Python 仓扩到双仓**：C# 仓没有组件决定表，MAF 1.23.0 已装，却存在多处自研等价物。

**三档分类**（判据不是「有没有框架」，是「框架给了什么、我们替它做了什么」）：

| 档 | 含义 | 处置 |
|---|---|---|
| **A 违反** | 框架／标准库已给完整实现，仍自研等价物 | 换掉；业务语义以配置或薄适配保留 |
| **B 边界** | 框架给机制／接口，自己给存储、策略、判据 | 保留但减薄，**禁止重写机制层** |
| **C 不违反** | 框架不给；或给了但语义冲突；或本身就是业务语义 | 自己写，必须登记理由 |

---

## 二、逐项裁决（2026-11-07 实测，路径＋行号）

### C# 仓（MAF 1.23.0 / MEA 已装）

| # | 现状 | 证据 | 档 | 处置 |
|---|---|---|---|---|
| 1 | Workflow 自研步骤状态作检查点，恢复＝重跑未完成步骤，**写类副作用会重复** | `Workflow/README.md:21,53` 自认「没用 MAF 的 `CheckpointManager`」 | **A** | 换 MAF checkpoint；只保留 `workflow_steps` 的业务状态与幂等键 |
| 2 | Workflow 无暂停／审批（README 列为 P5-1 未实现） | `Workflow/README.md:57` | **A** | 用 MAF HITL（`request_info` + response handler）；自研 `ToolApproval` 只保留业务规则 |
| 3 | ModelRuntime 自研重试循环：指数退避 `Math.Pow(2, attempt)` + `Task.Delay` | `ModelRuntimeChatClient.cs:65,119,213-227`；而 `Polly.Core` 已在 `Knowledge.Local` 引入 | **A** | 换 Polly resilience pipeline；只保留配置值与用量口径 |
| 4 | ToolExecution 自研管道／审批／白名单（7 文件） | `ToolExecutionPipeline.cs`、`ToolApproval.cs`、`ToolApprovalPolicy.cs` | **B** | 机制换 MAF function tools + middleware；保留白名单内容、fail-closed 规则、按人判定 |
| 5 | 错误响应是就地匿名对象 `new { code, message }`；`ProblemDetails` 全 src 零命中 | Webapi 8 控制器 | **A** | 用 ASP.NET ProblemDetails（RFC 9457）承载五字段 + `request_id` + `retryable`；形状不变、载体标准化 |
| 6 | 迭代／重复／超时守卫与终态归类码 | `AgentLoopOptions.cs`、`AgentRunGuard.cs` | **B** | 终止条件用 MAF；业务阈值、RunCode 归类、轨迹落库自己写 |
| 7 | 会话／记忆持久化 Store/Repository | `SqliteSessionStore`、`SessionStateRepository`、`SqliteLongTermMemoryStore` | **B** | 用 MAF `AgentSession` 的序列化格式；存储后端与归属仍自己写 |
| 8 | checkpoint 存储后端、幂等键＋去重表、审计事件表、参数校验规则、Step 三列 | ADR 0016 §四 3/4/5/6/7 | **C** | 框架不给，自己写 |
| 9 | **禁用 MEA `AIContextProviders`** | `AgentHarness/README.md:92`：会在外层再包函数环，绕过守卫与轨迹，全部退化成 `succeeded` | **C 例外** | 框架给了但语义冲突，**不得**按本篇启用；回归闸必修 |

### Python 仓

| # | 现状 | 档 | 处置 |
|---|---|---|---|
| 10 | 16 个 adopted 包：解析 `pypdf` / `python-docx` / `openpyxl`、切块 `llama-index-core`、迁移 `alembic`、队列 `huey`、加密 `cryptography`、口令 `argon2`、HTTP `fastapi`…；rejected 条目全带重开条件 | **合规** | 保持 |
| 11 | 自研 RRF 融合、两字切分、LIKE 兜底、判定／拒答／引用校验、删除三层补偿、评测脚本 | **C** | 保持。「判定与模型拿同一份证据」「补偿顺序」是业务语义，标准库不提供 |
| 12 | `rag/indexer.py` / `retriever.py` 自建双路索引 | **B（待验）** | LlamaIndex 有 query engine 标准形态，但换掉会作废 40 题基线与 0.05/0.45 阈值。按 ADR 0013 瘦 Python 时评估；重开条件：同一题库复测且不低于现状基线 |

---

## 三、对既有 ADR 的修订

- **ADR 0016 §四「补 7 项」重判**：第 1 项（HITL 挂起恢复）、第 2 项（Harness checkpoint）、第 6 项里的 middleware 挂点 → 从「补能力」改为「**启用 MAF 已给的机制**」（A/B 档）；第 3/4/5/7 项与审计事件表仍是 C 档自研。**7 项数量不变，工作量性质变了。**
- **ADR 0014 §六 改动清单符合本篇**：第 2/3/4 步本就是「对齐 MAF HITL／加 checkpoint 存储后端／加 middleware 管道」，不改。
- **ADR 0017 正交**：本篇管机制由谁实现，0017 管业务语义在哪。机制越标准化，语义越集中，两者互相加强。
- ADR 0013 的三个唯一不变；本篇不改变任何进程边界。

---

## 四、强制手段（真正的缺口在这）

现有 gate 只查「包」，查不出「框架给了能力却自己又写一遍」。要生效，需在 C# 仓 `ArchitectureTests` 补一条规则：**MAF/MEA 已提供标准机制的模块里，禁止出现自研等价物**——例如 `Workflow` 里出现自定义 checkpoint 状态机、`ModelRuntime` 里出现 `Task.Delay` 重试循环即失败。

落点已有先例：`ArchitectureBudgets.cs:104-111` 的正则扫源码 + 负控。新规则与 AC-02 禁词扫描**并列不合并**（一条管业务词，一条管自研机制）。

---

## 五、明确不做

- 不为「用框架」重写业务语义：白名单内容、fail-closed 规则、金额／枚举／跨字段校验、审计归属、判据与通过线仍自己定。
- 不因本篇放宽版本锁定：MAF **1.23.0 锁精确版本**，升级只在有明确理由时做，并跑动作回归清单。
- 不把「框架提供了」当成「必须用」：`AIContextProviders` 是实测会破坏 R1/R3 的反例，登记为 C 档例外。
- 不为贯彻本篇立刻替换 Python 检索实现（见 #12 的重开条件）。

---

## 六、后果与重开条件

**后果**：C# 侧机制层代码减少，剩下的是配置、业务规则、存储与判据；ADR 0016 的 7 项补缺工作量下降。但第一批那四件**一件不变**——让 `dotnet build` 通、挂 AC-02 禁词扫描 + 修两条缺陷、补回回归闸到 13/13，都不是框架能替的。

**重开本篇的条件**：出现一个框架能力，其默认行为与五条不变量（可停／可拦／可回放／可恢复／不自作主张）冲突，且无法通过配置或 middleware 纠正 → 记入 C 档例外并写明证据路径，**不得静默自研**。
