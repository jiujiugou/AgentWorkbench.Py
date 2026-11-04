# T-002 接入 LlamaIndex 切块器

**状态**：完成（2026-11-02）。四条完成条件全部达成，见文末「结果」

## 这一步产出什么

生产切块器从自写实现换成 LlamaIndex，评测链路跟着新块边界重跑，数字说话。

## 改哪些文件（白名单）

- `src/agentworkbench/ingest/parsing/chunker.py` —— 唯一的实现点，契约（页文本进、`ChunkDraft` 出）不变
- `pyproject.toml` —— llama-index-core：dependencies 声明 + 组件表 planned → adopted
- `evals/relabel_gold.py`（新）—— 题库重标工具
- `evals/self/questions.json` **不改**（原样搬迁，逐字节与 C# 一致）；重标结果写 `evals/self/questions-v2.json`（新文件）
- `evals/__main__.py` —— 题库路径指向 v2
- `docs/功能/F2-知识资料.md` —— 相关段落同步

## 题库重标方案（内容对齐，不人工重标）

gold 用 `(file, seq)`，块边界变了 seq 全变。对齐规则：
旧 gold 块的归一化文本在旧 fixture 库里有；重建新块后，找「内容包含旧 gold 块文本」的新块——
一个新块包含 → 直接换 seq；跨两个新块 → 两个都进 gold（等价于 ambiguous，语义合理）。
negative 题没有 gold，不用动。

## 明确不做

- **不加 `chunker_version` 列**（原 B0 待办）：fixture 库的 439 块是 C# 切的、不会重切，
  新块只在「新上传的文档」里产生。等第一次真换切块器重解析线上库时加才有意义，现在是空转。
- 不动解析层（C 选项 = T-003 的事）
- 不改 `evals/self/` 的任何文件

## 完成条件

1. 探针 `current` 模式（即生产实现）接缝率 = llama_default 的 7.52%
2. 全部不变量测试绿（现有断言都是相对断言：块数 >0、三处对齐，不吃死块数）
3. 题库重标完成，评测重跑，HitRate / MRR 与基线 0.812 / 0.588 对比写进 baseline.json
   ——**变好变差都要写理由**，README 的规矩
4. V-11 登记表按新数字更新（5% 门禁大概率仍不过，7.52%——原因在 T-001 写清了）

---

## 结果（2026-11-02）

| 完成条件 | 结果 |
|---|---|
| 探针 current = llama_default 接缝率 | ✅ 7.52%（532 块 40 处） |
| 不变量全绿 | ✅ 155 条（含 [d] 正控新测试） |
| 题库重标 + 评测重跑 | ✅ 0 失败：34 条 gold 对齐（exact 25 / head60 9 / 多块 1）；**HitRate 0.812→0.906、MRR 0.588→0.733** |
| V-11 登记表更新 | ✅ 7.52% 仍超 5%（C# 基线同样不过），剩余归解析层 |

**计划外的两个真收获**：

1. **生产 bug：chunks_fts.chunk_id 被写成字符串**（`str(chunk_id)`）。
   fts5 虚表列无类型，单表自洽看不出；评测跨表比对 `'487' in {487}` 永远 False，
   检索全 0 且不报错。修为 int，并在 [d] 正控测试里钉死类型断言。
2. **分词器与 C# 判据口径漂移**：ASCII 词切分（sqlite-wal 应为一个词）、
   单字中文、token 顺序、CJK 扩展 A 区范围，四处不一致导致一致性检查 [b] 类
   63 条误报 drift。tokenizer 对齐 C# `BigramTokenizer` 后 [a]~[e] 全 0——
   **V-10 的 [d] 类（正面 MATCH）随之通过，V-10 升 pass**。

**验证登记表现状**：V-1~V-10 全 pass；V-11 fail（7.52%>5%，归解析层）；
V-12 manual（未验）。
