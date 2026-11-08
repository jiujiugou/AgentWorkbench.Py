# ProblemDetails 替换设计方案（待审）

- **状态**：**已实施**（2026-11-07，用户「先开工吧」授权按方案默认值执行；实测结果见 §十）
- **日期**：2026-11-07
- **依据**：ADR 0018 §二 #5（A 档：框架已给完整实现却自研等价物）；`docs/实现/流程.md` 四步循环
- **实施对象**：C# 仓 `D:\Code\AgentWorkbench`（本方案与双仓架构正本同放 Py 仓 `docs/`）
- **硬约束（用户 2026-11-07）**：**不要耦合**

---

## 一、目标与范围

把 Webapi 控制器里手写的错误体统一换成 ASP.NET Core `ProblemDetails`（RFC 9457）。**只换载体，不改运行时行为**：状态码、业务码、人话消息、前端解析全部保持原样。

| 类别 | 数量 | 位置 |
|---|---|---|
| 匿名对象错误体（待换） | **22** | `WorkflowController` 7、`SettingsController` 5、`DocumentsController` 5、`AuthController` 3、`KnowledgeController` 2 |
| 已换成 `Result(...)` | 3 | `HarnessController.cs:25,36,40` |
| 裸空 body（可选，另立） | 15 | `NotFound()` / `Unauthorized()`，8 个控制器里的 7 个 |
| 业务结果伪失败（不换） | 1 | `SettingsController.cs:227`，`200 OK { ok=false, error }` |

**明确不做**（每条都是范围边界，不是遗漏）：

1. 不改前端 `web/src/api/{auth,settings,documents}.ts` 三份解析——这刀只动服务端响应体。
2. 不动 SignalR 错误通道 `Hubs/AgentHub.cs:340`（`SendAsync("Error", new { message })`）——它不是 HTTP，ProblemDetails 管不到。
3. 不加全局异常中间件 / `AddProblemDetails` / `UseStatusCodePages`——那会改变全局行为，另立一批。
4. 不动 `SettingsController.cs:227`——那是业务结果里的失败标志，不是协议层失败。
5. 不改 `Domain` 层任何错误码常量，不重新发明错误码体系。
6. 不修既有两条红灯 `DocumentContentViewTests.P03/P04`（与本批无关，单独跟踪）。

---

## 二、「不耦合」怎么定义

耦合在这里指三件事：**ASP.NET 类型渗进业务层**、**业务语义渗进通用载体**、**改一处要记得改 N 处**。用四条可机器判的不变量钉死：

| # | 规则 | 违反时的症状 | 怎么判 |
|---|---|---|---|
| **I1** | `Domain` / `Application` / 各业务模块源码中**零** `Microsoft.AspNetCore.*` 引用 | 换个 Web 框架要重写业务层 | `ArchitectureTests` 扫源码正则 |
| **I2** | `Controllers/` 下**零** `ProblemDetails`、`ObjectResult`、`Extensions[`、`new { message` | 序列化细节散落，改载体要改 22 处 | `ArchitectureTests` 扫源码正则（含负控标记） |
| **I3** | `Problems/` 下**零**业务码字面量、零 `ex.Message` 拼装、零 `DocumentErrorCodes` / `Workflow*` 引用 | 通用载体认识业务，新业务要回来改它 | `ArchitectureTests` 扫源码正则 |
| **I4** | 响应 JSON 顶层仍可平铺读到 `code` / `message` / `request_id` / `retryable`；前端三份解析零改动 | 契约悄悄变了，前端静默失效 | `ApiContractTests` 真序列化断言 |

**I3 的反面也要说清**：不耦合 ≠ 加一堆接口。不建 `IProblemFactory`、不建「万能错误工厂」把 22 处语义收进一个 switch——那才是真耦合。控制器保留各自的判定与文案，只把「怎么变成 HTTP 响应」交出去。

---

## 三、分层与文件清单

依赖方向单向，箭头只向下：

```
Controllers ──→ Problems/Result（唯一碰 ASP.NET 的地方）──→ ASP.NET Core
     │                          ▲
     └──→ 各业务模块的错误码常量（Domain 层，原地不动）
```

| 文件 | 动作 | 职责 | 允许依赖 |
|---|---|---|---|
| `Webapi/Problems/ApiProblem.cs` | 改 | 唯一适配器：`ApiError` → `ProblemDetails` → `ObjectResult` | 仅 ASP.NET Core |
| `Webapi/Problems/ApiError.cs` | **新增** | 纯 POCO：`Status` / `Code` / `Message` / `Retryable` / `Details` | 无（不引 MVC） |
| `Webapi/Controllers/*.cs` | 改 5 个 | 只判定「发生了什么」，调 `this.Result(...)` | Problems + 各自业务模块 |
| `Domain/Documents/DocumentErrorCodes.cs` | **不动** | 对外 UPPER_SNAKE 码的唯一来源 | — |
| `ApiContractTests/ProblemDetailsContractTests.cs` | **新增** | 契约 + 改坏验证 | 框架 `ProblemDetails` 类型 |
| `ApiContractTests/Support/F2Harness.cs` | 改 1 处 | `CodeOf()` 适配（见 D1） | 框架 `ProblemDetails` 类型 |
| `ArchitectureTests/ProblemDetailsDecouplingTests.cs` | **新增** | I1/I2/I3 三条禁令 + 负控 | — |

`ApiError` 放进 Webapi 的 `Problems/` 而不是 Domain：它不是业务概念，是 HTTP 边界的输入参数，不值得为它开新程序集。它不引 MVC，所以 I1 成立。

调用形态（控制器侧，改前 / 改后）：

```csharp
// 改前
return BadRequest(new { code = DocumentErrorCodes.NoFile, message = "请求里没有 file 字段" });

// 改后：码仍来自 Domain，载体由 Result 负责
return this.Result(new ApiError(400, DocumentErrorCodes.NoFile, "请求里没有 file 字段"));
```

`Result` 重载保持薄：`this.Result(ApiError error)` 与现有 `this.Result(int, string, string, ...)` 兼容，避免一次性推翻 `HarnessController` 已改的 3 处。

---

## 四、关键决策（带取舍）

### D1 · CLR 形状变了，JSON 形状不能变 —— 本方案最关键的一点

`F2Harness.cs:144-150` 的 `CodeOf()` 是反射取匿名对象的 `code` **属性**：

```csharp
return body.GetType().GetProperty("code")?.GetValue(body) as string;
```

`ProblemDetails` 没有 `code` 这个 CLR 属性（在 `Extensions` 字典里），换完后 `UploadValidationTests` 的 4 条 Theory（`NO_FILE` / `FILE_EMPTY` / `FILE_TYPE_UNSUPPORTED` / `FILE_TOO_LARGE`）会全红。

**处置**：`CodeOf()` 改为先从 `ProblemDetails.Extensions["code"]` 读，读不到再退回反射。它只依赖框架的 `ProblemDetails` 类型，**不依赖我们的 `ApiProblem`**——这是解耦，不是「改测试让它过」。

同时新增一条真序列化断言，用 `System.Text.Json` 把返回值序列化后断言顶层字段齐全。**只验 CLR 对象不够**：`F2Harness` 是 Controller 直调（注释已说明 413 需真管道），看不到 HTTP 头与序列化结果。I4 必须由序列化后的 JSON 证明，不是由 C# 对象属性证明。

**取舍**：不动 `StatusOf()`——它走 `ObjectResult` 分支，`Result` 仍返回 `ObjectResult`，天然兼容。

### D2 · 错误码归属：沿用 Domain，不搬进 Webapi

`Domain/Documents/DocumentErrorCodes.cs` 已经把约定写死了：对外 UPPER_SNAKE 码「一旦发布就不能改名」，内部 lower_snake 诊断码「只在日志与 detail 出现，不承诺稳定」，还区分了 Kestrel 413（无响应体、无码）。**这套约定直接沿用**，控制器继续引用 `DocumentErrorCodes.NoFile`，不复制、不改名、不在 `Problems/` 里重建一份。

Workflow 的 4 个强类型异常（`WorkflowDocumentNotFoundException` 等）尚未有对外码。本批**不新增码**：无码的失败 `code` 留空，保持与现状一致。新增对外码属于新增契约，按 `流程.md` 要单独走一轮。

### D3 · `request_id` 单点，不为它建接口

当前取 `HttpContext.TraceIdentifier`（`ApiProblem.cs:32`）。保持这一处取值，**不引入 `IRequestIdProvider`**——一个接口换不来可测性，只多一层间接。将来接统一请求中间件，只改这一行，并在注释里写死「不要在各控制器另造一套」。

需处理：框架 `DefaultProblemDetailsFactory` 会自动往 `Extensions` 塞 `traceId`，与我们自造的 `request_id` 语义重复。见 §八 待拍板。

### D4 · 异常映射留在控制器，不引入全局中间件

`WorkflowController` 是 4 个强类型 catch，`SettingsController` 是 2 个 `catch (Exception)`。**保留 catch 结构**，只把 `new { message = ex.Message }` 换成 `this.Result(...)`。理由：改成全局异常映射表会改变「哪些异常被吞、哪些冒泡」的行为，属于全局行为变更，不在本批。

`ex.Message` 允许继续进 `message`/`detail`（现状如此），但**禁止在 `Problems/` 里拼**——I3 管的就是这个。

### D5 · Content-Type 补齐

现有 `ApiProblem.cs:35` 用 `new ObjectResult(problem) { StatusCode = status }`，协商出来是 `application/json`，不是标准的 `application/problem+json`。给 `ObjectResult` 显式设 `ContentTypes`（或改用 `ControllerBase.Problem()`）。直调层断言 `ContentTypes` 集合，真管道层断言响应头。

### D6 · `detail` / `message` / `details` 三者

现状是 `message` 同时进标准 `detail` 与扩展 `message`，扩展里另有个恒为 `null` 的 `details`。**本批按 ADR 0018 保留五字段原样**（`details` 仍为 null），只补 D5 与 D1，不擅自改契约。是否收敛为「标准 `detail` + 兼容 `message`」见 §八。

---

## 五、对外的 JSON 契约（不变承诺）

| 顶层字段 | 来源 | 承诺 |
|---|---|---|
| `type` / `title` / `status` / `instance` | 框架 `ProblemDetailsFactory` | 新增，标准字段 |
| `code` | 各业务模块常量（可为 `null`） | **形状与取值不变**，前端可 switch |
| `message` | 控制器给的人话 | **不变**，前端三份解析只读它 |
| `details` | 恒 `null`（本批保留） | 不变 |
| `request_id` | `HttpContext.TraceIdentifier` | 不变 |
| `retryable` | 默认 `false`，504 为 `true` | 不变 |
| `Content-Type` | `application/problem+json` | **新增**（这是本批唯一的对外可见变化） |

---

## 六、执行顺序（对齐 `流程.md` 四步）

ADR 0018 已完成决策，无需新 ADR；本批属「新增契约」的中档，走 **① 定契约 → ② 写断言 → ③ 实现 → ④ 改坏**。

| 批 | 内容 | 退出条件（可复跑） |
|---|---|---|
| **P0** | ① 定契约（本文 §五）→ ② 写断言：`ProblemDetailsContractTests` + `ProblemDetailsDecouplingTests` → ③ 固化 `ApiProblem` / 新增 `ApiError` / 适配 `F2Harness.CodeOf` → ④ 改坏 | 断言先红在「类型不存在」而非「不知道测什么」；实现后全绿；§七 改坏表全红过一遍 |
| **P1** | `DocumentsController` 5 处 | `UploadValidationTests` 4 条 Theory 全绿，**且 `F2Harness.CodeOf` 的改动有序列化断言背书** |
| **P2** | `AuthController` 3 处 | 401 三条消息文案逐字不变 |
| **P3** | `KnowledgeController` 2 处 | `KnowledgeEntryPointTests` 不红 |
| **P4** | `SettingsController` 5 处 | `:227` 的 `200 OK { ok=false }` 保持原样 |
| **P5** | `WorkflowController` 7 处 | 4 个强类型 catch 的状态码逐一比对 |
| **P6（可选）** | 15 处裸空 body | 待 §八 拍板后另立 |

每批单独提交、可单独 revert。P0 是「先铺网再动控制器」——项目硬约定「先替换、后挂」，反过来会让 22 处改动没有验证兜底。

---

## 七、验证与改坏对照

当前 NuGet restore 故障（`XPlatMachineWideSetting` path1 为 null），暂用 `--no-restore`；restore 修好后补跑默认构建与全量测试。

```
dotnet build AgentWorkbench.slnx --no-restore
dotnet test tests/AgentWorkbench.ApiContractTests --no-restore
dotnet test tests/AgentWorkbench.ArchitectureTests --no-restore
```

| 改坏哪一行 | 必须红哪条 | 说明 |
|---|---|---|
| 删 `ObjectResult.ContentTypes` 赋值 | `P0_内容类型是_problem_json` | D5 不是空测试 |
| 删 `Extensions["message"]` | `P0_顶层_message_可读` + 前端兼容断言 | I4 的牙齿 |
| 删 `Extensions["code"]` | `UploadValidationTests` 4 条 | 现成契约网，不用另写 |
| 在 `Problems/` 里写死 `"NO_FILE"` | `I3_载体不含业务码` | 防通用载体反向认识业务 |
| 某业务模块 `using Microsoft.AspNetCore.Mvc` | `I1_业务层不引ASPNet` | 防框架类型下沉 |
| 控制器里直接 `new ObjectResult` | `I2_控制器只调Result` | 防 22 处再散一次 |
| 每条扫描规则配一个「绝不可能出现的标记」 | 负控返回 0 | 沿用 `ArchitectureBudgets.cs:96-98` 的写法，证明门禁不是恒真 |

**回归基线**：API 契约现有 39/41（两条红为既有 `DocumentContentViewTests.P03/P04`）、架构 12/13（回归闸指向已删工程）——本批不得让数字变差，修复这两项另立。

---

## 八、待你拍板（三件）

| # | 选项 | 建议 |
|---|---|---|
| **Q1** | 15 处裸空 body 是否本批一并收编？ | **否**。它涉及「404/401 要不要带 body」的语义决定，且可用 `UseStatusCodePages` 一次性兜，与本批「只换载体」不同性质，另立 P6 |
| **Q2** | 框架自动加的 `traceId` 与自造 `request_id` 留哪个？ | **留 `request_id`**（对外契约名，前端已读），把 `traceId` 从 Extensions 移除，避免一个响应两个 id。需你确认可接受「覆盖框架默认行为」 |
| **Q3** | 恒为 `null` 的扩展字段 `details` 保留还是去掉？ | **保留**（ADR 0018 #5 写的是五字段，去掉算契约变更）。若要收敛成「标准 `detail` + 兼容 `message`」，得先改 ADR 0018 的措辞 |

---

## 九、风险

1. **F2Harness 是 Controller 直调**，看不到真实 HTTP 序列化。D1 的真序列化断言是必须的，否则「JSON 形状不变」只是一句推理，不是证据。
2. **`ProblemDetailsFactory` 由 `AddControllers()` 默认注册**，但直调测试里 `Controller.ProblemDetailsFactory` 可能为 null（没有 DI 装配）。P0 必须确认这一点：`HarnessController` 已改的 3 处在直调层是否可测，若不可测需补最小装配，不能跳过。
3. **restore 未修**，`--no-restore` 能编译不等于 CI 能跑；本批不引入任何新包，正是为了绕开这个阻塞。
4. **两个仓各有前端**，本批只针对 C# 仓 `web/`；Python 仓 `web/src/api/http.ts` 的 `ErrorEnvelope` 是另一套信封，不受影响。

---

## 十、实施结果（2026-11-07）

用户未另行指定 Q1/Q2/Q3，按方案默认值执行：**Q1 否**（15 处空 body 不收编）、**Q2 留 `request_id` 并移除 `traceId`**、**Q3 保留恒为 null 的 `details`**。

### 实际改动（C# 仓 `D:\Code\AgentWorkbench`）

| 文件 | 动作 |
|---|---|
| `Webapi/Problems/ApiProblem.cs` | 改：唯一 `Result(ApiError)` 入口、补 `application/problem+json`、移除框架 `traceId`、合并 `Extra` |
| `Webapi/Problems/ApiError.cs` | **新增**：纯 POCO + `Extra` 数据通道 |
| `Controllers/{Documents,Auth,Knowledge,Settings,Workflow}Controller.cs` | 22 处匿名对象 → `this.Result(new ApiError(...))` |
| `Controllers/HarnessController.cs` | 已改的 3 处统一到 `ApiError`，全站共 **25 处**同一形态 |
| `ApiContractTests/ProblemDetailsContractTests.cs` | **新增**：7 条契约断言（真序列化、ContentTypes、五字段、前端兼容、traceId、retryable、无码场景） |
| `ArchitectureTests/ProblemDetailsDecouplingTests.cs` | **新增**：I1/I2/I3 + 负控，共 4 条 |
| `ApiContractTests/Support/F2Harness.cs` | 补 `ProblemDetailsFactory` 最小装配；`CodeOf` 改读 `Extensions["code"]`；迁移完成后删掉反射兼容分支 |

前端零改动，`SettingsController.cs:227`、SignalR `AgentHub.cs:340`、15 处裸空 body 均按范围外保留。

### 实测数字

| 项 | 基线 | 实施后 |
|---|---|---|
| `dotnet build` | 0 error | **0 error**（2 个既有 NU1902/NU1903 漏洞警告） |
| API 契约测试 | 39/41（2 红） | **46/48**（2 红）——新增 7 条全绿 |
| 架构测试 | 12/13（1 红） | **16/17**（1 红）——新增 4 条全绿 |
| 控制器匿名错误体 | 22 | **0** |
| `this.Result(new ApiError(...))` | 3 | **25** |

红项全部是既有、与本批无关：`DocumentContentViewTests.P03/P04`、`ArchitectureBudgets.Trace_replay_must_be_consumed_by_evaluation_tests`（回归闸指向已删的评测工程）。

复跑命令（restore 修好后去掉 `--no-restore`）。本机 `dotnet` 不在 PATH，需用绝对路径：

```
cd D:\Code\AgentWorkbench
"C:\Program Files\dotnet\dotnet.exe" build AgentWorkbench.slnx --no-restore
"C:\Program Files\dotnet\dotnet.exe" test tests/AgentWorkbench.ApiContractTests --no-restore
"C:\Program Files\dotnet\dotnet.exe" test tests/AgentWorkbench.ArchitectureTests --no-restore
```

### 改坏验证（9 条全红，源码字节级恢复 `True`）

| 改坏哪一行 | 结果 | 红在哪 |
|---|---|---|
| 删 `result.ContentTypes.Add(...)` | RED | `P0_内容类型是_problem_json`（1 红） |
| 删 `Extensions["message"]` | RED | 3 条 P0 契约 |
| 删 `Extensions["code"]` | RED | `UploadValidationTests` 4 条 Theory 全红 |
| 删 `Extensions.Remove("traceId")` | RED | `P0_不出现重复的_traceId` |
| 断开 `Extra` 合并 | RED | `DocumentDeleteTests` 2 条（残留行数字段） |
| 在 `Problems/` 写死 `"NO_FILE"` | RED | `I3_载体不含业务码` |
| 业务层出现 `using Microsoft.AspNetCore.*` | RED | `I1_业务层不引ASPNet` |
| 控制器出现 `new ObjectResult` | RED | `I2_控制器只调_Result` |
| 把负控标记改成真实存在的串 | RED | `扫描规则不是恒真门禁` |

### 与方案的偏差（如实记录）

1. **P0 顺序**：先固化载体 + 写全部断言（此时 I2 因控制器未改而红），再替换控制器让 I2 变绿。与「② 写断言 → ③ 实现」的理想顺序有偏差——I2 本身就是防控制器回归的闸门，天然要求「先替换、后挂」，沿用项目既有判断。
2. **新增 `ApiError.Extra`**：方案未预料到 `DocumentsController` 的 409 带 `ftsRemaining` / `vectorsRemaining`，而它们是**已发布契约**（`docs/验证与现状/验证方式.md:361,392`、`DocumentDeleteTests.cs:66,90` 明确断言）。加了纯数据通道，载体只合并键名、不解释；五字段后写以防 `Extra` 劫持契约。
3. **`Result` 重载**：方案说保留兼容重载，实施时删掉旧重载，`HarnessController` 3 处一并改用 `ApiError`，全站只留一个入口，避免双轨。
4. **`F2Harness` 装配**：§九 风险 2 预判到直调层 `ProblemDetailsFactory` 可能为 null，实测确实需要。补 `AddControllers()` + `DefaultHttpContext.RequestServices`，并在 `Dispose` 释放 `ServiceProvider`。

### 已知弱点 / 后续

- **I1/I2/I3 是源码文本扫描，注释里的禁词也会命中**。改坏验证第 7、8 条用注释形式触发，就是为了不破坏编译；这也暴露了判据的粗粒度。要精确需换成 ArchUnitNET 类型依赖判据（参照 `ConventionTests.cs`），另立一轮，不在本批。
- **15 处裸空 body 按 Q1 未收编**：这是「明确接受」，不是待办——404/401 要不要带 body 属于语义决定，且可用 `UseStatusCodePages` 一次性兜，与本批「只换载体」不同性质。
- **全局异常中间件 / `AddProblemDetails` / `UseStatusCodePages` 仍未启用**：框架自动产生的 400/404/500 仍是另一套形状。「全站统一」尚未成立，只是「手动 return 的 25 处统一」。
- 既有 2 条契约红、1 条架构红未修，与本批无关，另立跟踪。
