# C# 底座现状（2026-11-07 实测）

- **口径**：按 ADR 0016 的五条不变量、ADR 0017 的底座五层、v2.1 §5.1 A 组逐项核对；**只报事实，不重复论证**。
- **对象**：`D:\Code\AgentWorkbench`（17 工程 / `net10.0` / src 12,519 行 / tests 4,201 行）。
- **证据**：`reports/acceptance/2026-11-07/csharp-build.log`、`csharp-build-10.log`、`csharp-build-norestore.log`、`csharp-arch-tests.log`。

---

## 一、一句话

**运行时主体已在，动作保障全空；代码能编译，但默认构建卡在 NuGet restore 的环境层，且架构测试当前 12/13（回归闸是红的）。**

---

## 二、环境：落实代码之前的第一道门

| 项 | 实测结果 |
|---|---|
| dotnet | Git Bash 的 PATH 里没有；实际在 `C:\Program Files\dotnet\dotnet.exe` |
| SDK | 已装 3.1 / 6 / 8 / 9rc / **10.0.201** / 11.0.100-preview |
| `global.json` | **工作区已删除**（`git status` = `D`）；原内容指定 `10.0.201` + `rollForward: latestFeature`。删掉后默认选到 11 preview |
| 默认 `dotnet build AgentWorkbench.slnx` | **exit 1**：22 个工程全部 `NuGet.targets ... Value cannot be null (path1)`，无一条编译错误 |
| 换 10.0.201 后重跑 | 同样 22 个错误 → **不是 SDK 版本问题**（临时恢复 `global.json` 验证，已用 trap 还原，工作区状态未变） |
| 补 `LOCALAPPDATA` / `DOTNET_CLI_HOME` / `NUGET_PACKAGES` | 单工程 restore 仍报同错；用户级 `NuGet.Config` 内容正常 → **不是这几个变量、不是包源配置** |
| **`build --no-restore`** | **成功，0 错误 / 5 警告**（1m43s）。22 个 `project.assets.json` 已在 → **C# 代码本身可编译** |
| `test ArchitectureTests --no-build` | **12 通过 / 1 失败**，红的是 `ArchitectureBudgets.Trace_replay_must_be_consumed_by_evaluation_tests` |

> 结论：**不是代码坏了，是 restore 环境坏了**。改代码前先让它恢复，否则任何改动都无法用构建验证。

---

## 三、五条不变量逐项（ADR 0016 §二）

| | 状态 | 关键位置 |
|---|---|---|
| **R1 可停** | ✅ 已成立 | `AgentLoop/AgentLoopOptions.cs:11/14/24/31`（6 轮 / 30s / 重复 3 次 / token 预算）；`AgentRunGuard.cs:36-42/45-58`；`Shared/Execution/AgentRunStatus.cs:11-40`（8 终态）、`AgentRunCode.cs:13-44`（10 码）；`AgentHarnessService.cs:363-378` 把「跑满被逼收口」记成 `iteration_limit`、正常答完记 `succeeded` |
| **R2 可拦** | ⚠️ 半成立 | `ToolExecution/ToolExecutionPipeline.cs:142-201`（调用前守卫）、`Safety/ToolPolicy.cs:22-35`（白名单热重载）、`:184` 调用后 `IToolResultVeto`、`ToolCatalog.cs:37-40` 注册期裁剪、`:214-255` fail-closed 审批。**业务参数校验与按人权限判定零命中**（`Validate` / `permission` / `ClaimsPrincipal` / `CurrentUser` 实查）；`Webapi/Auth/CurrentUser.cs` 只用于会话归属，未进 `HarnessRunRequest` |
| **R3 可回放** | ⚠️ **实现在、防线失效** | `Observability/ExecutionTrace.cs`、`ExecutionTraceStore.cs:18-88`（Save/Get/Query 齐全）、`ToolExecution/ToolExecutionPipeline.cs:41-47` `ToolCallTrace`。**但回归闸所指的 `tests/AgentWorkbench.Evaluation.AgentTask` 工程已不存在**，`ArchitectureBudgets.cs:85-98` 因此变红（见 §六） |
| **R4 可恢复** | ❌ 不成立 | Harness run 无 resume / 无断点快照（`AgentHarness/README.md:105` 自述）；Workflow 层 `Workflow/Execution/WorkflowStepExecutorNode.cs:25-26` 以 `workflow_steps.succeeded` 跳过已完成步，`WorkflowService.cs:164-193` 有 `RerunStepAsync` / `ResumeAsync`——**但恢复 = 重跑未完成步骤，不是快照续** |
| **R5 不自作主张** | ✅ 已成立 | `AgentHarnessService.cs:341-344` 用户原文直接进模型；`RunInstructionsChatClient.cs:18-38` 只插 system；`ContextOptions.cs:7-9` 记录 `AgentWorkbench.Context` 项目在阶段 4 已删；`slnx` 无该工程 |

---

## 四、底座五层 → 工程（ADR 0017 §二口径）

| 层 | 工程 / 文件 |
|---|---|
| Runtime / Harness | `AgentHarness`（`AgentHarnessService.cs` 540 行）、`AgentLoop`、`ToolExecution`（管辖半边）、`ModelRuntime`、`Providers`、`Conversation/ChatService.cs` |
| 编排 | `Workflow`（`WorkflowService.cs`、`Execution/WorkflowGraphBuilder.cs`、`WorkflowStepExecutorNode.cs`、`Steps/AgentStepExecutor.cs`） |
| 知识（底座侧） | `Knowledge.Abstractions`（`IKnowledgeEngine.cs` / `Retrieval/*`）；`Knowledge.Local` 与 `Documents` 的工具/解析**按 ADR 0013 属业务侧适配器候选**，不是底座 |
| 记忆与状态 | `Memory` + `Memory.Persistence`（`SqliteSessionStore.cs` 等）、`Persistence`（`AppDbContext` / `AgentRunRepository` / M001~M027）、`Domain/Observability/AgentRunRecords.cs` |
| 工程保障 | `Observability`（`ExecutionTrace*`）、`Shared`（终态 / 归类码 / 用量口径）、`ArchitectureTests`、5 个测试工程、`Webapi/Program.cs` 组合根 |

**业务词零命中**（ADR 0017 AC-02）：`PurchaseOrder` / `Supplier` / `AmountLimit` / `TaxCode` / `ERP` / `SAP` / `Modbus` 在 `src/` 全部 0 文件 → **底座业务无关这条当前成立**。

---

## 五、七个补缺项现状（ADR 0016 §四）

| 项 | 状态 | 证据 |
|---|---|---|
| HITL 挂起→人批→恢复 | **有基础但缺** | `ToolExecutionPipeline.cs:229-239` 是 `WaitAsync` 同步占连接；`ToolApproval.cs:25` 只有契约，**全 src 无 `IToolApprover` 实现**；`Workflow/README.md:57` `awaiting_approval` 未实现 |
| Harness 层 checkpoint | **零命中** | `AgentHarnessService.cs` 全文无 `resume` / `checkpoint` / `Snapshot` |
| 幂等键 + 去重表 | **零命中** | M001~M027 无对应表；`Idempotency` / `dedup` 全 src 零命中；四个 README 自述缺失 |
| 业务参数校验 | **零命中** | `ToolExecution/README.md:41` 明写「取值范围、路径合法这类业务校验由每个工具自己写」 |
| 权限前移到调用前 | **零命中** | 现有只有工具名级白名单；无 user / tenant 维度 |
| 审计表 + 五字段信封 + `request_id` | **零命中** | `Program.cs:104-117` 无 `UseExceptionHandler` / `AddProblemDetails`；8 个控制器全是就地匿名对象 |
| `AgentStepRecord` 三列 | **零命中** | `Domain/Observability/AgentRunRecords.cs:20-32` 只有 Id/RunId/Iteration/Kind/Tool/Arguments/Result/Error/CreatedAt |

---

## 六、必修缺陷（本次新发现 3 条 + 已知 2 条 + 1 条）

| # | 缺陷 | 证据 |
|---|---|---|
| 1 | **回归闸失效，且当前是红灯** | `ArchitectureBudgets.cs:88` 扫 `tests/AgentWorkbench.Evaluation.AgentTask`，该目录不存在 → 架构测试 12/13。ADR 0016 §二「R3 有回归闸」据此已更正 |
| 2 | **审批闸门有，但没人能开** | 全 src 零 `IToolApprover` 实现；`HarnessController.cs:29`、`Hubs/AgentHub.cs:116-117`、`Workflow/Steps/AgentStepExecutor.cs:48` 三个 run 入口都没传 `Approver` → 实跑 `Ask` 工具必得 `approval_unavailable`。与 v2.1 §5.1 补的「审批挂起了却没人能批」是同一断点 |
| 3 | **Workflow 恢复会重放副作用** | `Workflow/README.md:53` 原文：「恢复实际是重跑未完成步骤，不是从快照续；写类副作用会重复」，`:54` 承认没有幂等键。与 ADR 0016 B2「放弃自动重放带副作用的工具调用」直接冲突 → **属缺陷，不属放弃** |
| 4 | 工具重名 fail-open | `ToolCatalog.cs:38` `DistinctBy(tool => tool.Name)` 静默去重，无重名检测；`README.md:92` 自列未改 |
| 5 | 参数留痕丢类型 | `ToolExecutionPipeline.cs:276` `_ => value?.ToString()`，存成 `{"failTimes":"0"}` 而非 `{"failTimes":0}`（`README.md:40`） |
| 6 | 错误响应无统一信封 | 8 控制器全部 `new { code, message }` / `new { message }`；`request_id` / `retryable` / `traceId` 在 Webapi 零命中 |

---

## 七、AC-02 禁词扫描的现成挂点

- `tests/AgentWorkbench.ArchitectureTests/ArchitectureBudgets.cs:104-111` 已有**正则扫源码**的先例（`Regex.Count` + 排除 bin/obj），`:97` 还带负控断言；`ConventionTests.cs:44-48` 有 ArchUnitNET 类型依赖写法。
- 现有架构测试守 4 组规则：`LayeringTests`（Domain 纯净 / 不反向依赖 Webapi / Memory 不依赖 Persistence）、`ModuleCycleTests`（无环）、`ConventionTests`（控制器不计时 / 长驻工作 / 仓储带事务）、`ArchitectureBudgets`（规模与棘轮）。**没有业务词禁词规则**，但挂得进去。

> 另注：C# 仓库 `docs/adr/` **只有 README.md**，0013~0017 的正本都在 `AgentWorkbench.Py/docs/adr/`（按 ADR 0013 只存一份的决定）。实施时须跨仓库看，别在 C# 侧找这几篇。

---

## 八、第一批建议只做三件（未实施）

1. **让 `dotnet build` 恢复**（修 restore 环境 / 恢复 `global.json` / 用标准 Windows 终端跑一次）；否则改代码无法验证。
2. **挂 AC-02 禁词规则 + 修缺陷 4、5**：两处都有精确落点，不动运行时骨架，且能立刻把「可拦 / 可回放」的底线钉住。
3. **补回回归闸**，让架构测试回到 13/13；这是 R3 唯一的防退化保险。

七项补缺（§五）等基线绿了再排——尤其第 1 项 HITL 与第 2 项 checkpoint 都依赖审批链路，先修 §六 第 2 条「没人能批」才有意义。

---

## 九、与其他文档的关系

- 判据来自 ADR 0016（五不变量 / 补 7 项 / 放弃 18 项）、ADR 0017（底座业务无关 / AC-01 / AC-02）、v2.1 §5.1 A 组。
- **ADR 0016 §二 R3 的「已成立，且有回归闸」已按本次实测更正**：实现在，回归闸失效且当前为红灯。
- 本文是现状盘点，不是设计；不替代 `docs/架构/能力矩阵-七层填空.md`（那是双仓能力对照与归属裁决）。
