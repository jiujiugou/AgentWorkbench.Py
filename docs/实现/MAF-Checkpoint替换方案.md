# MAF CheckpointManager 替代方案（待审）

- **状态**：**已实施**（2026-11-07；Q1~Q4 按方案默认值执行，实测与偏差见 §十一）
- **日期**：2026-11-07
- **来源**：用户 2026-11-07 12:34 指令「那我们就使用 maf 提供能力了，自己不做，先写替代方案」
- **依据**：ADR 0018 §二 #1（A 档）、ADR 0016 R4 / B2 / RR-3 / RR-4、F9 REQ-SAFE-01/03
- **实施对象**：C# 仓 `D:\Code\AgentWorkbench`（本方案与双仓架构正本同放 Py 仓 `docs/`）
- **框架版本**：`Microsoft.Agents.AI.Workflows` **1.23.0**（精确锁定，不升级）

---

## 一、这次到底换什么（先把边界说死）

**换的是「执行进度由谁保存、由谁决定从哪续」，不是换掉数据库里的步骤记录。**

| | 现在 | 换完 |
|---|---|---|
| 恢复的唯一权威 | `workflow_steps.status == succeeded` | **MAF checkpoint 快照** |
| `workflow_steps` 的角色 | 检查点（既判跳过又做展示） | **业务台账**：只供 REQ-18 查询、REQ-19 断连可查、前端进度展示、审计 |
| `ResumeAsync` 干什么 | 把 run 改回 `pending` 重新入队，整图从头新建 | 按 `sessionId` 找最新断点，`ResumeStreamingAsync` 从快照续 |
| `WorkflowStepExecutorNode.cs:25-26` | `if (succeeded) return message;` ← 自研检查点 | **删掉**。放行由引擎决定，节点不再自己判 |

**「自己不做」的准确含义**：不复刻**机制层**（superstep 边界判定、快照序列化、pending 消息/请求/共享状态的保存与恢复、断点索引、恢复时事件重发、HITL 请求响应通道）。框架不给的**业务语义与存储后端仍要自己写**——这不是违背决定，ADR 0018 已把它们判为 C 档。

---

## 二、框架给什么 / 自己写什么

### 框架给的（一律不自研，1.23.0 实测 API）

| 能力 | API（XML 文档行号） |
|---|---|
| 启用检查点 | `InProcessExecution.RunStreamingAsync<T>(Workflow, T, CheckpointManager, string sessionId, CancellationToken)`（`:4090`）；`RunAsync<T>(..., CheckpointManager, ...)`（`:4099`） |
| 从断点恢复 | `InProcessExecution.ResumeAsync(Workflow, CheckpointInfo, CheckpointManager, CancellationToken)`（`:4102`）；`ResumeStreamingAsync(...)`（`:4093`） |
| 找断点 | `CheckpointManager.GetLatestCheckpointAsync(sessionId, ct)`（`:1816`）；`run.Checkpoints`（`:1190`） |
| 持久化 store | `FileSystemJsonCheckpointStore(DirectoryInfo)`（`:1340`）、`JsonCheckpointStore`、`ICheckpointStore<JsonElement>`（`:1422`） |
| 组装 manager | `CheckpointManager.CreateJson(ICheckpointStore<JsonElement>, JsonSerializerOptions)`（`:1807`）；`CreateInMemory()` 仅测试用 |
| 拿快照引用 | `SuperStepCompletedEvent.CompletionInfo?.Checkpoint`（`:6680, :6721`），配合 `StreamingRun.WatchStreamAsync()`（`:6533`） |
| executor 自定义状态 | `Executor.OnCheckpointingAsync` / `OnCheckpointRestoredAsync`（`:2489, :2498`）+ `IWorkflowContext.QueueStateUpdateAsync` / `ReadStateAsync`（`:4244, :4195`） |
| HITL 通道（为 P2 预留） | `RequestPort` / `ExternalRequest.Create` / `RequestInfoEvent` / `StreamingRun.SendResponseAsync(ExternalResponse)`（`:5411, :3270, :5396, :6512`） |

### 框架不给、必须自己写（C 档，ADR 0018 #8）

| # | 自己写什么 | 为什么框架不管 |
|---|---|---|
| 1 | **幂等键 + 去重表** | 跨系统「这一笔是否已执行」是业务事实，MAF 没有 ERP 概念 |
| 2 | **超时只查证不重放**的业务判据 | B2 / F9 REQ-SAFE-03；框架恢复默认会 replay pending events |
| 3 | `workflow_steps` 台账写入（状态/产物/轨迹） | REQ-18/19 要能按 runId 查；框架快照不是给人查的 |
| 4 | `Step` 加 `IdempotencyKey` / `ApprovalId` / `ExternalRef` | ADR 0016 §四 第 7 项 |
| 5 | `JsonSerializerOptions`（自定义消息类型） | `CreateJson` 文档明写「using custom types in messages or state 时必须提供」 |
| 6 | store 目录、权限、备份策略 | 部署决定 |

**这一节是方案里最容易被误读的地方**：「自己不做」≠ 零代码量，而是「机制不自己造」。

---

## 三、状态源分工（唯一需要设计的架构点）

```
MAF checkpoint ── 执行进度的唯一权威 ──→ 决定从哪一步续
       │                                      │
       └── SuperStepCompletedEvent ───────────┘
                                              
workflow_steps (SQLite) ── 业务台账 ──→ REQ-18 查询 / REQ-19 断连可查 / 前端进度 / 审计
                            ▲
                            └── 由 executor 照常写，但**不参与放行判定**
```

两条硬规则：

1. **台账不决定恢复**。判据：把所有 `workflow_steps.status` 手工改成 `succeeded`，恢复后未完成步骤**仍必须执行**（这条要做成负控测试，见 §八）。
2. **两者允许短暂不一致、最终必须一致**。checkpoint 在 superstep 边界提交，DB 在步骤级写；恢复后节点会重新走 `HandleAsync` 并覆盖状态，因此**不需要额外的「台账对齐」动作**——只要删掉跳过判断即可。要断言的是「恢复跑完后台账与最终结果一致」，不是「每一刻都一致」。

这直接推翻 README 红线 2「检查点只有一个来源」的原表述。新表述应为：**执行进度只有一个来源（MAF 快照）；台账是它的只读投影，不是第二个判据。**

---

## 四、逐文件改造清单

| 文件 | 改什么 |
|---|---|
| `WorkflowServiceCollectionExtensions.cs` | 注册 `FileSystemJsonCheckpointStore`（单例、`IDisposable` 由宿主释放）+ `CheckpointManager.CreateJson(store, options)`；`options` 覆盖 `WorkflowStepMessage` 的自定义类型 |
| `WorkflowService.ExecuteAsync` | `RunAsync` → `RunStreamingAsync`（带 manager + `sessionId: runId.ToString()`）+ `WatchStreamAsync` 事件循环；`SuperStepCompletedEvent` 里记 `Checkpoint`；入口先用 `GetLatestCheckpointAsync(sessionId)` 判断「新跑 / 恢复」 |
| `WorkflowService.ResumeAsync` | 不再「整图重入」：仍改 run 状态并入队，实际恢复由 `ExecuteAsync` 的断点分支完成；**Worker 与队列零改动** |
| `Execution/WorkflowStepExecutorNode.cs` | 删 `:25-26` 的 `if (succeeded) return message;`；状态照常落库；超时分支改为「不重试、只查证」（见 §六 P0） |
| `Execution/WorkflowStepMessage.cs` | 不变，但**必须**进 `JsonSerializerOptions`（含 `WorkflowRunRecord` / `ChunkRecord` / `List<WorkflowStepRecord>`、`required` 属性） |
| `Execution/WorkflowGraphBuilder.cs` | 修 `:10` 注释漂移（现在写「检查点全部交由 MAF」，改完才名副其实）；executor id 保持 `definition.Key` 不变 |
| `Domain/Workflow/WorkflowRecords.cs` | 修 `:13`、`:62` 注释里的「检查点」措辞；`WorkflowStepRecord` 加三列（P0） |
| `Workflow/README.md` | 重写红线 2、§一「检查点与重跑」、§二未解决问题表第 53/54 行 |
| `docs/adr/0018...md` | 按 §九 改判 #1（待用户确认后单独提交） |

### 关键形态（伪代码，签名以 1.23.0 XML 为准）

```csharp
// ExecuteAsync 入口：一处判断新跑还是恢复
var sessionId = runId.ToString();
var latest = await _checkpoints.GetLatestCheckpointAsync(sessionId, linked.Token);
StreamingRun stream = latest is null
    ? await InProcessExecution.RunStreamingAsync(workflow, message, _checkpoints, sessionId, linked.Token)
    : await InProcessExecution.ResumeStreamingAsync(workflow, latest, _checkpoints, linked.Token);

await foreach (var evt in stream.WatchStreamAsync(linked.Token))
{
    if (evt is SuperStepCompletedEvent done)
        _logger.LogDebug("checkpoint {Id} at step {N}", done.CompletionInfo?.Checkpoint, done.StepNumber);
    // RequestInfoEvent → HITL 挂起（P2）
}
```

`sessionId: runId.ToString()` 现在就已经在传（`WorkflowService.cs:246`），**不用改数据结构、不用加 checkpoint_id 列**——这是整套替换里最省的一处。

---

## 五、四个待拍板的设计点

| # | 问题 | 选项 | 建议 |
|---|---|---|---|
| **Q1** | **`RerunStepAsync` 怎么活？** 现在语义是「重置该步 + 下游 stale + 整图重入」，与「从最新 checkpoint 续」直接冲突；框架没有删除 checkpoint 的 API | R1 建 `workflow_checkpoints(session_id, checkpoint_id, step_key)` 映射表，按 stepKey 找断点恢复<br>R2 **单步重跑 = 新建 run**（旧 run 归档），零映射表<br>R3 本期不提供单步重跑 | **R2**：最符合「自己不做」，语义清晰；代价是 API 从 `void` 变成返回新 `runId`，前端要跟 |
| **Q2** | store 选型 | `FileSystemJsonCheckpointStore`（框架给，落磁盘）<br>自定义 `ICheckpointStore<JsonElement>` 落 SQLite | **FileSystemJsonCheckpointStore**。ADR 0013 的「唯一状态持有者」指 C# 进程，不要求单一介质；先别自己实现 store。注意 XML 明写「**not thread-safe**」，单例访问要串行化（现有单读者队列刚好满足） |
| **Q3** | 台账由谁写 | A executor 照常写（现状，最薄）<br>B `SuperStepCompletedEvent` 驱动，天然对齐边界 | **A**。B 要重写进度通知与状态机，收益只是「每刻一致」，而 §三 已论证不需要每刻一致 |
| **Q4** | HITL 是否本批 | 本批一起做<br>另立项 | **另立项**。`IToolApprover` 零实现、前端无审批 UI，是独立大工程。本批只把 checkpoint 打通，使 HITL **成为可能**；`RequestInfoEvent` 的挂点留一行注释位 |

---

## 六、准入条件（硬闸）：P0 不做完，P1 不许上

前面审查已确认：**换框架不会自动消灭重复写**，框架恢复反而会 replay pending events。所以下面两件必须在启用 checkpoint **之前**完成：

1. **修 `ExecuteWithRetryAsync` 的超时重放**（`WorkflowStepExecutorNode.cs:96-102`）：现在 `TimeoutException` / 非用户取消的 `OperationCanceledException` 会在 `attempts < maxAttempts` 时再调一次 `executor.ExecuteAsync`。改成「超时不重试，只查证」（B2 / F9 REQ-SAFE-03）。**这是当前代码里已经存在的自动重放，与检查点无关，但必须一起修。**
2. **幂等键 + 去重表 + Step 三列**（RR-3 / F9 REQ-SAFE-01）。Persistence 里 `Idempot` / `checkpoint` 全零命中，属于从零建。

**验收判据**：同一 `request_id` 连打两次写动作，ERP 侧只产生一次副作用；超时后不产生第二次调用。这两条绿了才允许进 P1。

---

## 七、执行顺序与退出条件

| 批 | 内容 | 退出条件 |
|---|---|---|
| **P0** | 修超时不重放 + 幂等键/去重表/Step 三列 + 补契约断言 | 上述两条判据绿；改坏验证红过一遍 |
| **P1** | DI 注册 store/manager；`ExecuteAsync` 换 `RunStreamingAsync` + 事件循环；删节点跳过判断；`ResumeAsync` 走断点分支 | §八 全部测试绿；`workflow_steps` 全改 `succeeded` 的负控仍红（证明权威已换） |
| **P2** | 跨进程恢复验证（新 store 实例读旧断点） | 进程重启后从断点续，不重跑已完成步骤 |
| **P3** | 文档修订：README 红线 2、`WorkflowRecords.cs:13,62`、`WorkflowGraphBuilder.cs:10` | 全仓不再有「步骤状态 = 检查点」的表述 |
| **P4（另立项）** | HITL 挂起 → 人批 → 恢复 | 不在本方案验收范围 |

每批独立提交、可单独 revert。**P0 是 P1 的准入，不是并行项。**

---

## 八、验证与改坏对照

现状：`README.md:58` 自己写着「本模块几乎没有测试」，只有 `WorkflowStepRegistryTests`。**必须先补网再动代码**（沿用 `docs/实现/流程.md` 四步：定契约 → 写断言 → 实现 → 改坏）。

新增 `tests/AgentWorkbench.ApplicationTests/Workflow/WorkflowCheckpointTests.cs`：

| 测试 | 判什么 |
|---|---|
| 正例 | 跑到第 2 步中断，恢复后探针显示只执行第 3 步 |
| **负控（最关键）** | **把 `workflow_steps` 全部改成 `succeeded`，恢复后未完成步骤仍必须执行** —— 证明放行判据已从 DB 状态换成 checkpoint，不是换个名字继续自研 |
| 负控 | 不传 `CheckpointManager`（退回 `RunAsync` 旧重载）→ 恢复用例必须红 |
| 跨进程 | 用新 `FileSystemJsonCheckpointStore` 实例读旧断点并恢复 |
| 超时不重放 | `maxAttempts=2` + 超时，副作用探针计数仍为 1（B2） |
| 幂等键 | 同一 `request_id` 两次写动作，ERP 侧只一次（RR-3） |
| 台账最终一致 | 恢复跑完后 `workflow_steps` 与引擎终态一致（REQ-18/19） |

改坏对照表（每条都要能说出「改这处 → 红哪几条」）：

| 改坏哪一行 | 必须红 |
|---|---|
| 删 `RunStreamingAsync` 的 `CheckpointManager` 参数 | 恢复正例 + 负控 |
| 把 `if (succeeded) return message;` 加回节点 | 全改 `succeeded` 的负控（证明自研检查点复活） |
| 恢复分支改回「改 pending + 重新入队」 | 恢复正例 |
| `CreateJson` 不传 `JsonSerializerOptions` | 跨进程恢复（自定义类型反序列化失败） |
| 改掉 executor id（`definition.Key`） | 恢复用例（官方：拓扑与 id 必须一致） |
| 超时分支恢复重试 | 超时不重放 + 幂等键两条 |
| 删幂等键去重 | RR-3 用例 |

复跑命令（restore 修好后去掉 `--no-restore`；本机 `dotnet` 不在 PATH）：

```
cd D:\Code\AgentWorkbench
"C:\Program Files\dotnet\dotnet.exe" build AgentWorkbench.slnx --no-restore
"C:\Program Files\dotnet\dotnet.exe" test tests/AgentWorkbench.ApplicationTests --no-restore
"C:\Program Files\dotnet\dotnet.exe" test tests/AgentWorkbench.ArchitectureTests --no-restore
```

基线不得变差：API 契约 46/48（2 红为既有 P03/P04）、架构 16/17（1 红为回归闸指向已删工程）。

---

## 九、对 ADR 0018 #1 的改判建议（本轮不改，待你确认）

现文：「没用 MAF `CheckpointManager` → 写类副作用会重复 → 换 MAF checkpoint；只保留 `workflow_steps` 的业务状态与幂等键」。

**前半句成立，归因错位**：重复写的根因是「外部写先于状态落库」+「超时自动重试」，不是「检查点由谁实现」。框架官方文档明写 checkpoint 在 superstep 结束后创建，恢复时 `ICheckpointingHandle.RestoreCheckpointAsync` 会 re-emit pending events——**框架给的是至少一次，不是恰好一次**。

建议拆成两条：

| 档 | 条目 | 处置 |
|---|---|---|
| **A** | 未启用 MAF checkpoint | 换。收益是「HITL 挂起跨天恢复 + 引擎状态恢复 + 精确断点」，**不是**修重复写 |
| **C** | 写副作用恰好一次 | 框架不给。靠幂等键 + 超时只查证不重放（B2 / RR-3 / RR-4 / F9 REQ-SAFE-01/03），与 ADR 0018 #8 同档 |

并补一条执行约束：**A 与 C 必须成组，C 在先**。

---

## 十、风险与已知不做

1. **序列化**：`WorkflowStepMessage` 含 `required` 属性与多个自定义类型，不给 `JsonSerializerOptions` 会在恢复反序列化时炸。P1 第一条就该验这个。
2. **executor id 稳定性**：官方要求恢复时拓扑与 id 完全一致。现在 `definition.Key` 稳定 ✓；但**工作流定义或提示词版本一变，旧 checkpoint 就失效**，需要版本兼容策略（本批先登记为已知限制，不设计）。
3. **store 不是线程安全的**：`FileSystemJsonCheckpointStore` XML 明写。现有 `WorkflowWorker` 单读者队列刚好串行；若将来加并发（README §二 第 56 行列为待办），必须同步加锁。
4. **`CreateInMemory` 禁用**：进程重启即丢，不满足 R4，也违反 ADR 0013。
5. **范围外**：HITL 具体审批器实现、`IToolApprover` 落地、前端审批 UI、并发上限、输入源抽象（README §二 第 55 行）、既有 3 条基线红灯。都不夹带进本批。
6. **文档漂移三处**（`WorkflowGraphBuilder.cs:10`、`WorkflowRecords.cs:13,62`、README 红线 2）必须在 P3 一起改，否则会出现「框架说继续、文档说只有一个状态源」的分裂。

---

## 十一、实施实测（2026-11-07）

### 结果

| 项 | 结果 |
|---|---|
| `build AgentWorkbench.slnx --no-restore` | **0 error**（6 个既有警告） |
| `AgentWorkbench.ApplicationTests` | **167/167**（新增 `WorkflowCheckpointTests` 7 条） |
| `AgentWorkbench.DataIntegrationTests` | **22/22**（新增 `WorkflowIdempotencyStoreTests` 4 条） |
| `AgentWorkbench.ApiContractTests` | 46/48，2 红为既有 `P03/P04`，基线未变差 |
| `AgentWorkbench.ArchitectureTests` | 16/17，1 红为既有回归闸指向已删评测工程 |
| `AgentWorkbench.EndToEndTests` | 该工程当前无测试 |
| **改坏验证** | **6/6 RED**，源码字节级恢复 `True`（对照表见 `src/AgentWorkbench.Workflow/README.md` §五） |

Q1=R2（单步重跑新建 run）、Q2=`FileSystemJsonCheckpointStore`、Q3=executor 照常写台账、Q4=HITL 另立项，均按方案默认值执行。

### 实施中撞到、方案未写到的六件事

1. **非 streaming 的 `RunAsync(..., CheckpointManager, ...)` 不 commit checkpoint**。实测带 manager 的 `RunAsync` 跑完，`index.jsonl` 仍是 0 字节；只有 `RunStreamingAsync` + `WatchStreamAsync()` 消费事件才在每个 superstep 结束时落断点。方案 §四 的伪代码选 streaming 是对的，但 §二 把 `RunAsync` 并列为可用 API 会误导——它不能作为恢复依据。
2. **`Workflow` 实例被 runner 独占**：对同一张图调用 `ResumeStreamingAsync` 抛 `Cannot use a Workflow that is already owned by another runner`。恢复必须**重建一张同拓扑的图**；官方「拓扑与 executor id 必须一致」的实际落点在这里。`WorkflowService.ExecuteAsync` 本来就每次 Build，天然满足。
3. **`FileSystemJsonCheckpointStore` 是进程独占且实现 `IDisposable`**：同进程内对同一目录 new 第二个实例会抛「store is already in use」。跨进程测试改为先 `Dispose()` 释放锁再重建；生产以单例注册，`WorkflowWorker` 单读者队列串行访问。
4. **幂等语义比方案更强**：不只超时，「任何开始过却没走到 Complete 的重放」都由 `INSERT OR IGNORE` 的零影响行触发转 `unknown`，拒绝自动再写。原因是框架恢复本来就会 re-emit pending events，只拦超时挡不住崩溃/进程被杀那一支。查证后用 `ResolveAsync` 解成 `completed`，恢复时复用 `external_ref` 而不产生第二次副作用。
5. **恢复后的消息是 checkpoint 反序列化的副本**，不是原 `WorkflowStepMessage` 对象。因此 `ExecuteAsync` 的终态判断改为**回库重读** `GetStepsAsync(runId)`；测试也从「断言原 Message」改为「断言最后一次 `UpdateStepAsync` 快照」（Q3 只要求最终一致，不要求同一对象）。
6. **真实 store 测试抓到一个真 bug**：`WorkflowIdempotencyStore` 的 `SELECT` 依赖 `AddNitroPersistence` 里才设置的全局 `Dapper.DefaultTypeMap.MatchNamesWithUnderscores`，脱离 DI 的宿主会静默把 `external_ref` 读成 `null`。已改为显式列别名，不依赖全局状态。

### 其他落地偏差

- `RerunStepAsync` 按 R2 把 `IWorkflowService` 签名从 `Task` 改成 `Task<long>`；`WorkflowController` 的 202 体变成 `{ runId = 新runId, stepKey, previousRunId = 原id }`，前端不读新字段也能用，但语义已变。
- `WorkflowStepRecord` 加三列后，`DataIntegrationTests` 手写建表 DDL 同步补列（否则 `no column named approval_id`）；迁移是 M028。
- restore 仍坏（`XPlatMachineWideSetting` path1 为 null），测试工程为在 `--no-restore` 下拿到 MAF 类型，除了 `ProjectReference` 还必须显式加 `PackageReference Include="Microsoft.Agents.AI.Workflows" Version="1.23.0"`。
- **ADR 0018 #1 的改判（§九）未执行**，仍待你确认；本批只改代码与实现文档。
- 遗留 U-26（service 级测试缺口）、U-27（HITL）、U-28（版本兼容 / store 并发）已写入 `docs/验收/未解决问题.md`。

### 复跑

```
cd D:\Code\AgentWorkbench
"C:\Program Files\dotnet\dotnet.exe" build AgentWorkbench.slnx --no-restore
"C:\Program Files\dotnet\dotnet.exe" test tests/AgentWorkbench.ApplicationTests --no-restore
"C:\Program Files\dotnet\dotnet.exe" test tests/AgentWorkbench.DataIntegrationTests --no-restore
"C:\Program Files\dotnet\dotnet.exe" test tests/AgentWorkbench.ApiContractTests --no-restore
"C:\Program Files\dotnet\dotnet.exe" test tests/AgentWorkbench.ArchitectureTests --no-restore
```
