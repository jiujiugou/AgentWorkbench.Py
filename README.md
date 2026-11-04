# AgentWorkbench · Python 实现

C# 版（`../AgentWorkbench`）的重写。**文档继承，代码重写。**

## 为什么重写

C# 版把 F3 知识问答做到了可运行、可验收（21 项里 11 项有牙齿、端到端 2 条），
但 RAG 这一层的成熟工具几乎全在 Python：切块（LlamaIndex）、评测（RAGAS）、
重排（FlagEmbedding）、文档解析（unstructured）。自研的代价已经具体可见——
切块 7.29% 从半句开始、引用校验在相邻功能码上差 0.091 分不开。

## 什么继承、什么重写

| 继承（资产，与技术栈无关） | 重写（非资产） |
|---|---|
| 需求 REQ-04 ~ REQ-22 与判据 | 全部 C# 代码 |
| F3 的 21 项子问题拆解 | 数据库 schema |
| 验证方法、通过线定义、改坏法 | 前端（是否保留待定） |
| 决策记录 D-01~D-12、假设 A-01~A-07 | |
| 40 题评测集与 C# 基线（命中率 0.812 / MRR 0.588，439 块库） | |

## 文档地图

| 文件 | 只回答 |
|---|---|
| `docs/需求/产品目标与范围.md` | 这是什么产品、边界在哪 |
| `docs/需求/功能需求.md` | 六个功能域各自的 REQ |
| `docs/需求/阶段1功能需求.md` | 阶段 1 验收条目与状态 |
| `docs/功能/00-功能域怎么写.md` | 一个功能域该产出哪四份、信息往哪流 |
| `docs/功能/00-放这里.md` | 新信息归哪份的五个问句 |
| `docs/功能/F1-身份与访问.md` | F1 做什么、选哪些组件、哪些是加分项、怎么验 |
| `docs/功能/F2-知识资料.md` | 同上（F2）——切块器与题库的先后顺序在这里 |
| `docs/功能/F3-知识问答.md` | 同上（F3）+ 四段流水线、判定、引用两级校验 |
| `docs/功能/F4-执行与审计.md` | 同上（F4）——无后门与状态机迁移 |
| `docs/功能/F5-会话与记录.md` | 同上（F5）——引用快照与消息↔运行关联 |
| `docs/功能/F6-服务端能力.md` | 同上（F6）——断连容忍与平台级模型配置 |
| `docs/功能/F3-功能验证.md` | F3 的 21 项验到哪、红了算什么（明细在此，不在上面那份） |
| `docs/功能/F3-决策记录.md` | 为什么这么定、否掉了什么、哪些还是赌注 |
| `docs/功能/代码规范.md` | 代码怎么写、为什么这么写——只写工具判不了的部分 |
| `docs/实现/流程.md` | **一次改动怎么落地**：分档、四步循环、什么时候删文档段落 |
| `docs/验收/上线验收计划.md` | **这次怎么判定能否上线**：P0/P1、四层证据、10 天计划 |
| `docs/adr/README.md` | 决策记录（ADR）的格式约定 |

## 怎么跑

### 部署（一条命令从零到界面）

```bash
# 0) 依赖：Python 3.12+，Node 仅在要重建前端时需要
python -m venv .venv
.venv\Scripts\pip install -e .          # Windows；Linux/macOS 用 .venv/bin/pip

# 1) 密钥（缺了启动即失败，不会带病运行）
set AWB_SECRET_KEY=换成一段随机长字符串   # PowerShell 用 $env:AWB_SECRET_KEY=...

# 2) 建管理员（只能成功一次；口令走环境变量或交互输入，不收 --password）
set AWB_BOOTSTRAP_PASSWORD=你的口令
.venv\Scripts\python scripts\bootstrap_identity.py

# 3) 前端产物（已有 web/dist 可跳过）
cd web && npm install && npm run build && cd ..

# 4) 启动
.venv\Scripts\python -m agentworkbench serve            # http://127.0.0.1:8000
.venv\Scripts\python -m agentworkbench serve --host 0.0.0.0 --port 8080   # 局域网访问
```

建表在启动时自动完成（幂等）；**老库**升级结构用 `.venv\Scripts\alembic upgrade head`。

### 备份与恢复

```bash
# 备份（热备，服务运行中可做）：库的一致副本 + 上传文件 → 一个目录
.venv\Scripts\python -m agentworkbench backup --to D:\backup\20261104

# 恢复（先停服！）：把备份目录装回数据目录
.venv\Scripts\python -m agentworkbench restore --from D:\backup\20261104
```

- 备份目录是自足的：`agentworkbench.db` + `files/`（上传原件），整个目录拷走即完成异地保存
- `--to` 指向已存在且非空的目录会拒绝执行（不许覆盖上一份备份）
- **换机器恢复必须设置同一个 `AWB_SECRET_KEY`**，否则供应商凭据解不开
- 定时备份建议用系统计划任务/cron 包上面的 `backup` 命令，保留策略自定

### 开发

```bash
python scripts/ci.py check      # 本地 CI：九段（gates/format/lint/imports/types/tests/web×2）
python scripts/ci.py fix        # 能自动修的先修，再校验
python scripts/ci.py audit      # 每周：依赖卫生 + 漏洞

python -m evals run             # 跑评测并写基线，离线约 3 秒，不调模型
python -m evals run --no-write  # 只跑不写，改完代码先看数
python -m evals show            # 打印当前基线
```

评测按来源分目录，互不覆盖：

| 目录 | 放什么 | 进版本库 |
|---|---|---|
| `evals/self/` | 自建 40 题 + 5 个原样搬迁的评测脚本（与 C# 仓库逐字节一致，不再修改） | 是 |
| `evals/public/` | 公开基准（RGB 拒答子集、C-MTEB 重排），待接 | 是（数据另说） |
| `evals/fixtures/` | 库副本（10MB 二进制） | 否 |
| `evals/reports/` | 每次运行的原始输出 | 否 |
| `evals/baseline.json` | **当前标准**——数字变了就是一次需要理由的提交 | 是 |

## 移植时必须知道的三件事

**1. `F3-功能验证.md` 的「实现类 · 方法」列全是 C# 的名字**

`SufficiencyGate.Judge`、`ToolExecutionPipeline`、`CitationVerifier.Verify`、`ChatMessage.vue`
——这些在 Python 版要么改名要么不存在。重写后必须重填这一列，不能照抄。

**2. 那份文档的「状态」列记录的是 C# 版的结论**

通 / 不通过 / 半通 / 未验 / 缺，全部属于 C# 实现。**Python 版代码一行没写，等于全部未验。**
读的时候把状态列当历史结论看，不要当成本仓库的现状。

**3. 有若干链接指向从未创建的文档**

`用户与权限.md`、`验收条件.md`、`待确认决策.md`、`非功能需求.md`、`接口.md`
——这五份在原仓库就不存在（需求文档里预留的占位）。移植时保持原样，没有编造补写。
`docs/adr/README.md` 里列的 0001~0007 同样只有目录没有正文。

## 当前状态

文档已移植（9 份，约 1670 行）。代码：39 个模块文件 + 本地 CI + 评测入口，**无业务实现**。

已可复现的数字（`python -m evals run`，2026-11-02，见 `evals/baseline.json`）。
T-002 起评测对准 llama 库（LlamaIndex 切块 532 块）；C# 基线（439 块库 + self 原版）
保留可对账：

| 项 | llama（当前） | C# 基线 |
|---|---|---|
| 索引一致性 | 63 文档 / 532 块 / 三处全等，5 类检查 0 条不一致 | 63 / 439 / 0 条 |
| 检索命中（TopK=5） | **HitRate 0.906 / MRR 0.733**（literal 0.857 / paraphrase 0.913 / ambiguous 1.000） | 0.812 / 0.588 |
| 切块接缝率 | 7.52%（40/532，目标 ≤5% 未达） | 7.29% |
| 负控 8 条 | 7 条召回——检索层没有拒答能力，拒答只能靠判定层（REQ-09） | 同 |

切块器换了（自写 11.66% → LlamaIndex 7.52%），检索随之变好（+0.094 / +0.145）。
接缝率的剩余部分归解析层决定（表格/代码行没有句号，按句切救不了）。

下一步按 F3-功能验证 §7 的顺序走，但每条都要先在这里重新实现：

1. ~~切块换成成熟切分器~~（T-001/T-002 完成：LlamaIndex SentenceSplitter 800/120）
2. 检索强制发生（原实现 4 次问答里 1 次零检索）
3. 拒答阈值定标（原实现 `RefuseMargin` / `SufficientDistance` 为空，判定永远落在"未判定"）
