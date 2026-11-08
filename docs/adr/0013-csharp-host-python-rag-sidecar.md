# ADR 0013 · C# 主服务持有全部状态，Python 侧进程只做无状态 RAG

- **状态**：Accepted（设计决定，尚未实施）｜ **日期**：2026-11-06
- **覆盖范围**：`../AgentWorkbench`（C#）与 `../AgentWorkbench.Py`（Python）两个仓库。**本文是这条分工的唯一正本**，不在 C# 仓库复制一份，避免两处漂移。
- **机制**：本文 §2 数据归属、§3 内部契约、§4 任务清单。判据写在 §1。

## 背景

两个仓库是**同一需求的两次实现**，不是两个互补模块：2026-10-31 把 C# 库的 61 份文档 / 437 片段迁到了 Python 库，代码不共享，也不能同时跑。

Python 重写时把 Agent 本体整块砍掉了：`agent/tools.py`（13 行）与 `agent/loop.py`（10 行）只剩 docstring，
`StepKind.TOOL` 全项目只在枚举定义出现一次，`agent_steps.tool_name` 列从未被填过，前端 `toolSteps`
展示管道完好却永远没有数据。而 C# 版本来就有 `AgentHarness` / `AgentLoop` / `Conversation` / `ModelRuntime` /
`ToolExecution` / `Workflow` / `Memory` / `Memory.Persistence` / `Observability` 这些工程。

代价是 Python 侧用 `mypy strict`、101 处 `cast` / `Protocol` / `Any`、20 条 gates、23 篇文档、113 条验证项
去补编译器本来给的东西；而这套系统的真实使用痕迹是 2 条会话、6 条消息。工程约束越堆越厚，产品本身没有往前走。

## 一、决定与判据

**C# 主服务是唯一对外入口、唯一状态持有者、唯一权限判定点（PDP）。
Python 侧进程只提供无状态计算能力，通过内部 API 被调用，不对外暴露、不判身份、不落业务状态。**

边界判据只有一条：**给定输入必然得到同一输出、且「发生过以后要查／要回滚」的东西不存在 → Python；
否则 → C#。** 不是「AI 归 Python、业务归 C#」——检索产生的证据快照也是状态，模型调用本身也不属于 RAG。

由此推出三条硬约束：

1. **模型 client 在 C# 侧**。工具调用要求 C# 能发带 `tools` 的请求、收 `tool_calls`；
   留在 Python 就会让每次工具决策跨进程往返一次，边界立刻破。C# 已有 `ModelRuntime`。
2. **编号分配、判定、装填、引用校验在 C# 侧**。`packing.py` 的 D-03 要求「编号、判定、模型拿的
   出自同一份证据」，跨进程后只能由持有 `run` 的一侧一次固定并落快照。
3. **权限的 PDP 只有一处，PEP 散在各层**。`tenant_id` / `kb_ids` 由 C# 判定后作为查询条件传入，
   Python 只执行过滤，不读身份、不判角色。

## 二、数据归属：拆成两个库，不共享连接、不跨库事务

当前 Python 库 32 张表把两侧数据混在一起，这是实施时第一个要动的地方。

| 归属 | 表 | 说明 |
|---|---|---|
| **C# 业务库**（唯一状态源） | `tenants`、`users`、`account_levels`、`invites`、`tickets`、`identity_events`、`admin_events`、`system_state`、`knowledge_bases`、`conversations`、`messages`、`message_citations`、`agent_runs`、`agent_steps`、`providers`、`provider_models` | 身份、权限元数据、会话、**引用快照**、执行轨迹、供应商与模型配置。共 16 张 |
| **Python 知识库**（可重建资产） | `documents`、`document_pages`、`chunks`、`chunks_fts` 及 5 张影子表、`chunks_vec`、`vec_chunks_vec` 及 4 张影子表、`sqlite_sequence`；以及 `data/uploads/` 文件本体 | 文档、片段、全文与向量索引。共 16 张，**全部可由原件重建** |

- `knowledge_bases` 含 `visibility` / `min_level_rank`，是权限元数据，**归 C#**；Python 只收 `kb_ids`。
- `message_citations` 是引用快照，归 C#。C# 用 `doc_id` + `chunk_id` 指向 Python 侧的证据，
  **不复制正文**，但快照（当时显示的原文片段、文件名、序号、页码）必须落在 C#，否则原文被删后历史答案无法复核。
- `providers` / `provider_models` 归 C#（模型调用在那侧）；**embedding 配置例外**——
  `AWB_EMBEDDING__*` 是 Python 侧自己的本地配置，不进业务库。
- 两侧各自持有 `AWB_SECRET_KEY` 或等价物；Python 侧不需要解任何业务凭据。

## 三、内部契约（四条，均为内部端口，不对外暴露）

| 接口 | C# → Python | Python → C# | 明确不带什么 |
|---|---|---|---|
| `/retrieve` | `question, tenant_id, kb_ids[], top_k, request_id` | `hits[]`：`chunk_id / doc_id / content / page_from / page_to / seq / vector_distance / fts_score` | 不带身份、不带判定结果、不带 `[n]` 编号、不带阈值 |
| `/embed` | `texts[], request_id` | `vectors[]` | 不带文档状态 |
| `/ingest` | `file, doc_id, request_id` | `chunks[] + 索引结果` | 不带权限、不带删除决策 |
| `/execute` | `action, params（已校验）, idempotency_key, request_id` | `result, external_ref, elapsed_ms` | 不带身份判定、不带重试决策 |

- `request_id` 贯穿每次跨进程调用与后续所有 `run` / `step`，是查轨迹的唯一线索。
- Python 侧错误返回结构化三态：`retryable` / `terminal` / `partial`。**是否重试、run 收成什么终态，
  由 C# 侧状态机决定**，Python 不自行重试。
- 跨进程超时会出现**状态未知**（Python 到底执行了没有）。因此 C# 侧必须带幂等键、去重表与
  超时后的补偿动作；这归 `AgentLoop` / `AgentRunGuard` 管，不归 Python。
- MAF 的 checkpointing 只恢复**框架内部工作流状态**，不保证 Python 侧没被重入，别指望它替管这层。

## 三·附：工具执行体可以放 Python，工具的管辖不能

「工具交给 Python」要拆成两层，否则会破 §1 的第 2、3 条硬约束。

| 层 | 归属 | 内容 |
|---|---|---|
| **工具管辖** | **C#** | 工具注册表与白名单、JSON Schema、参数校验、权限判定、幂等键生成、去重表、审计落库 |
| **工具执行体** | **Python（可以）** | 拿到已校验的参数，翻译成 ERP 的 HTTP / SDK 调用，把结果原样返回 |

理由：执行体是纯翻译，放哪都行，Python 调 ERP 的库更多、改起来不用动主服务；
但**写操作是唯一有外部副作用的东西**，它的校验、幂等、审计不能跑在工程约束最弱的一侧。
幂等键必须与 `request_id` 同源，否则重试对不上；权限判定必须与 PDP 同源，否则出现两个判定点。

`/execute` 的契约见 §三。三条约束：

1. **Python 侧无状态**：不存 `idempotency_key → 结果` 的映射；重复调用就重复转发。去重归 C# 的去重表。
2. **超时窗口不靠重试解决**：C# 记「执行中」，恢复时**查 ERP 的实际状态**，不重放。§3 已经写明的「状态未知」处理不因 `/execute` 改变。
3. **只读与写分开**：只读工具的校验可以一并放 Python；**写工具的校验必须在 C#**。

由此 Python 侧从「RAG 进程」变成「能力进程」，四个内部端点：`/retrieve`、`/embed`、`/ingest`、`/execute`。

## 四、任务清单（按依赖顺序，不是排期）

### C# 主服务（`../AgentWorkbench`）

| # | 任务 | 依赖 |
|---|---|---|
| C1 | 成为唯一对外入口：接管身份、登录、权限 PDP、管理后台与全部业务 API | — |
| C2 | 定义 §3 内部契约与 `request_id` 传播 | C1 |
| C3 | 恢复 Agent 本体：`AgentLoop`（模型循环 + 迭代守卫）、`ToolExecution`（白名单 / 调用前守卫 / 调用后否决）、`AgentHarness`（上下文）、`Workflow`（图式编排 + Checkpoint + 人工审批） | C1 |
| C4 | `ModelRuntime` 接现有智谱 GLM（`glm-4-flash`），**先实测 tool calling 稳定性**；不支持就换模型，不要靠 prompt 硬撑 | C2 |
| C5 | 业务库接管 §2 的 16 张表；实现引用快照、幂等键、跨进程失败后的 run 收口 | C2 |
| C6 | 判定 / 编号 / 装填 / 引用校验从 Python 迁过来（对应 `judgment.py` / `sufficiency.py` / `packing.py` / `citation.py` / `veto.py`） | C2, C5 |
| C7 | 长期记忆与会话状态：`Memory` / `Memory.Persistence` / `Persistence` 接回主链路 | C5 |
| C8 | Tracing / 审计 / Evaluation 入口统一到 C# 侧 | C5 |

### Python 侧进程（`../AgentWorkbench.Py`）

| # | 任务 | 依赖 |
|---|---|---|
| P1 | 瘦成无状态服务：只保留 `/retrieve`、`/embed`、`/ingest`、`/execute` 四个内部入口 | C2 |
| P2 | 保留 `rag/`（retriever / indexer / tokenizer / freshness）、`ingest/`（parsing / pipeline / storage / deletion）、`llm/embeddings.py`、`evals/`、以及 61 份文档与 437 片段 | — |
| P3 | 退役或移交：F1 身份、F4 轨迹、F5 会话、F7 通用对话、settings / 供应商配置、45 个端点中的大部分、`web/` 前端 | C1, C5 |
| P4 | 移除判定阈值、`[n]` 编号分配、证据快照逻辑（迁往 C#） | C6 |
| P5 | 评测资产保留并改造成「对 Python 侧 `/retrieve` 的契约评测」：40 题、HitRate / MRR、切块接缝率、引用范围校验 | P1 |
| P6 | 删除业务状态表（§2 的 16 张），只留知识库 16 张 | C5 |

**顺序理由**：C# 侧先立起「唯一状态源 + 契约」，Python 侧才有明确的退役边界；反过来先瘦 Python，
会出现一段时间两侧都没有身份与 run，任何一次失败都查不回来。

## 五、明确不做（防止两种说法并存）

- 不让两个进程同时持有业务状态，不共享 SQLite 文件，不做跨库事务。
- 不让 Python 侧判身份、判角色、收 `users` 表。
- 不在 C# 侧重写文档解析、切块、向量与全文索引。
- 不引入 LangGraph：编排在 C#，对应物是 MAF `Microsoft.Agents.AI.Workflows` 与现有 `Workflow` 工程。
- 不把内部 API 暴露到公网；Python 侧进程只允许本机 / 内网访问。

## 六、后果

**换来的**：状态只在一处；编译器、程序集边界、状态机重新接管兜底；C# 版被砍掉的 Agent 本体
不用重写，是**迁移**；Python 只做它擅长的无状态计算，不再用 101 处 `cast` 和 23 篇文档硬撑工程性。

**代价（不粉饰）**：这不是「扩展 Python 版」，是**重新划界**，Python 从主服务降为能力进程。
F1 / F4 / F5 / F7 的实现、前端、45 个端点里的大半会退役；113 条验证项里围绕这些域的判据要重立。
`docs/验收/上线验收计划.md`（v2.4）与 `docs/验收/现状.md` 描述的是**单进程 Python 版**的验收口径，
实施本决定后需要单独标记「不适用」，不能继续当作现行结论。

**仍然成立、要带走的**：`rag/` / `ingest/` / `embeddings.py` 及其判据（切块接缝 7.52%、
HitRate / MRR、引用范围校验、40 题评测集），以及「判据先于实现、改坏验证、问题台账」这套方法论。

## 七、实施前的待处置（不属于验收台账，不进 `未解决问题.md`）

1. 双库拆分与迁移脚本：现有 61 份文档 / 437 片段留在 Python，业务数据回迁 C#，引用快照要重建。
2. C4 的 tool calling 实测：这是整条路的第一个可证伪点，不通过则要重议 C3。
3. `web/` 前端归属：主服务变 C# 后，前端应只调 C# API；是改造还是随 Python 版一起退役，未定。
4. C# 侧 `AWB_SECRET_KEY` 与 Python 侧 embedding 配置的部署边界。
5. 验证台账重立：哪些判据继续有效、哪些随单进程形态作废，需要一条条过，不能整表继承。

## 八、重开条件

- 若 Python 侧开始需要会话状态、长期记忆或 Checkpoint → 说明「状态 vs 无状态」这条判据被破坏，重开。
- 若出现必须跨库原子一致的场景（例如删除文档与吊销引用必须同事务）→ 重议数据归属，
  优先考虑把该能力整体收回 C#，而不是引入分布式事务。
- 若 C# 侧最终没有接管模型调用（tool calling 留在 Python）→ 本决定的第 1 条硬约束失效，整篇重开。
