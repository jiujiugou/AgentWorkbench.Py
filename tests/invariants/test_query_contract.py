"""不变量：查询串的构造必须与索引、与评测口径三方一致。

失效形态（用户会看到什么）：问一句正常的中文长问题，系统回答「资料不足」——
而库里明明有那段话。原因不是没搜到，是查询串把「必须同时出现」当成了规则：
中文长问句切出的 bigram 里有「是什」「么原」这种跨词组合，任何文档都不会同时
具备，AND 语义下必然 0 命中，而且不报错、不告警、测试全绿。

三层测试都没抓到它，因为输入恰好都绕开了：生产单测用「FC01 读线圈」（3 个
token，AND 也能中）、端到端用「ADDR-005」（1 个 token，必中）、评测脚本自己
实现了一套 OR 构造、根本不调生产代码。所以本文件补三条：

① 长问句必须命中（行为守门）
② 单字查询必须走 LIKE 兜底（短查询守门）
③ 生产与评测对同一查询命中同一批块（口径守门——防两边再次背离）
④ 评测脚本的「索引文本」复刻必须与生产 `to_index_text` 逐字节一致

第③④条比前两条值钱：前两条只守「今天这个错」，后两条守「以后任何一侧改动」。

第④条的由来（2026-11-02 实修）：评测脚本的格式检测器把索引文本算成
「去重版」的 bigram，而生产写入时不去重。于是每块只要有重复 bigram，
比对必然失败，检测器就把**正确**的索引报成「原文 trigram」，还建议换
`--tokenizer legacy` 重跑——按它说的改才会真的 0 命中。数字一直是对的
（0.906 / 0.733），错的是那句警报。

改坏这个不变量会怎样（本轮实测）：把 `to_match_query` 的 `OR` 改回空格（FTS5 的
AND），长问句立刻 0 命中，`test_long_chinese_question_must_hit` 与
`test_match_query_uses_or_not_and` 同时红——这两条就是那次修复留下的锁。

服务于：G3（40 题可复算）、G4（装填的前提是先有证据）
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path
from types import ModuleType

import pytest
from conftest import insert_document

from agentworkbench.core.documents import ChunkDraft
from agentworkbench.core.tokenizer import to_index_text, to_match_query
from agentworkbench.db import engine
from agentworkbench.llm.embeddings import HashEmbedder
from agentworkbench.rag.indexer import search_fts, write_index

# 一句正常的中文长问句：八个 bigram 里有四个（Modbus / 是什 / 什么 / 么原）
# 在下面这块文档里不存在——AND 必 0 命中，OR 必命中。
LONG_QUESTION = "Modbus 异常码 0x02 是什么原因"
CHUNK = "异常码 0x02 表示非法数据地址，检查从站地址与数据长度是否匹配。"


TENANT = "default"
"""这些测试的数据租户。检索必填它，夹具就把数据造在这里。"""


@pytest.fixture()
def indexed() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    engine.load_vec(conn)
    engine.create_schema(conn)
    embedder = HashEmbedder()
    draft = ChunkDraft(seq=0, page_from=1, page_to=1, content=CHUNK)
    insert_document(conn, "doc-1", tenant_id=TENANT)
    write_index(
        conn,
        document_id="doc-1",
        drafts=[draft],
        embeddings=embedder([CHUNK]),
        now="2026-11-02T00:00:00",
    )
    conn.commit()
    return conn


def _eval_harness() -> ModuleType:
    """加载 vendored 评测脚本（只 import，不改它一个字节）。"""
    path = Path(__file__).resolve().parents[2] / "evals" / "self" / "run_rag_eval.py"
    spec = importlib.util.spec_from_file_location("run_rag_eval_vendored", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_long_chinese_question_must_hit(indexed: sqlite3.Connection) -> None:
    """长问句必须命中——AND 语义下这条必红（本轮修的真 bug）。

    这不是「多召回一条」的优化，是「有没有」的分界：0 命中时判定层收到空列表，
    按设计必须判「资料不足」。于是用户看到的是「资料里没有」，而资料就在库里。
    """
    hits = search_fts(indexed, LONG_QUESTION, tenant_id=TENANT, limit=5)
    assert hits, "库里明明有这块，长问句却 0 命中：查询串的布尔语义错了"


def test_match_query_uses_or_not_and() -> None:
    """构造出的 MATCH 串必须是 OR——空格在 FTS5 里等于 AND，这是踩过的坑。

    断言的是布尔语义而不是字符串形状：八个 token 里只要有任意一个命中就算。
    """
    match = to_match_query(LONG_QUESTION)
    assert " OR " in match, f"查询串里没有 OR，FTS5 会当成 AND：{match}"


def test_single_han_char_falls_back_to_like(indexed: sqlite3.Connection) -> None:
    """单字查询（如「站」）不产生 bigram，必须靠 LIKE 兜底才有结果。

    C# 的口径是「MATCH 命中 0 才走 LIKE，不把兜底当主路」。这条守着兜底存在。
    """
    hits = search_fts(indexed, "站", tenant_id=TENANT, limit=5)
    assert hits, "单字查询没有任何结果：LIKE 兜底没接上"


def test_production_query_matches_eval_harness(indexed: sqlite3.Connection) -> None:
    """生产与评测脚本对同一查询命中同一批块——口径背离的唯一防线。

    评测脚本是 C# 原件（逐字节保留，用来对账基线），它带着自己那套查询构造。
    两边靠人工保持一致的地方，迟早会漂；这条把「漂了」变成当场红。
    """
    harness = _eval_harness()
    queries = [LONG_QUESTION, "异常码", "FC01 读线圈", "数据地址", "站"]
    drifted: list[str] = []
    for query in queries:
        production = {hit.chunk_id for hit in search_fts(indexed, query, tenant_id=TENANT, limit=5)}
        evaluated = set(harness.fts_search(indexed, query, 5, "bigram"))
        if production != evaluated:
            drifted.append(f"{query}: 生产 {sorted(production)} ≠ 评测 {sorted(evaluated)}")
    assert not drifted, "生产与评测口径已背离：\n" + "\n".join(drifted)


# ── 索引文本：评测脚本的复刻必须与生产逐字节一致（第④条）──

SAMPLES = [
    # 有重复 bigram（「地址」两次）——去重版的复刻会在这里露馅
    "异常码 0x02 表示非法数据地址，检查从站地址与数据长度是否匹配。",
    # 型号与连接符：H5U / sqlite-wal / 0x02 各是一个词
    "H5U 在 sqlite-wal 模式下把 0x02 写入 ADDR-005。",
    # 标点与换行：索引文本里它们不产生 token，但会切开汉字串
    "维护保养（每季度）\r\n清洁散热风道。",
]


def _eval_harness_v2() -> ModuleType:
    """加载 v2 评测脚本（实际跑的那一版）。"""
    path = Path(__file__).resolve().parents[2] / "evals" / "run_rag_eval_v2.py"
    spec = importlib.util.spec_from_file_location("run_rag_eval_v2_loaded", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_eval_index_text_matches_production() -> None:
    """评测脚本算出的索引文本必须与生产 `to_index_text` 逐字节相同。

    这不是洁癖：格式检测器靠这个函数判断「库里的索引是 bigram 还是原文」，
    算错就报警报反——把正确索引报成不一致，然后建议你换 `--tokenizer legacy`。
    真照着做，查询和索引才真的对不上，结果是 0 命中且没人知道为什么。
    """
    for harness in (_eval_harness(), _eval_harness_v2()):
        for text in SAMPLES:
            assert harness.index_text(text) == to_index_text(text), (
                f"{harness.__name__} 的索引文本与生产不一致：{text!r}"
            )


def test_deduped_variant_is_what_broke_the_checker() -> None:
    """去重版（查询侧形态）必然与索引文本不同——这就是那次误报的机制。

    钉住它，是为了让「顺手复用一个现成的分词函数」这个念头当场被拦住：
    查询要的是去重且限长的短列表，索要的是**原样**的全部 token。
    """
    harness = _eval_harness()
    text = SAMPLES[0]
    deduped = " ".join(harness.bigram_terms(text, limit=10**9))

    assert deduped != to_index_text(text)
    assert deduped.count("地址") == 1 and to_index_text(text).count("地址") == 2


@pytest.mark.parametrize("harness_name", ["v1", "v2"])
def test_index_format_check_accepts_a_production_built_index(
    indexed: sqlite3.Connection,
    capsys: pytest.CaptureFixture[str],
    harness_name: str,
) -> None:
    """用生产代码建出来的索引，检测器必须说「一致」。

    负控就是上面两条：一旦把 `index_text` 换回去重版，这条会打印 `!!`
    （用户看到的是评测脚本说自己的库格式不对，然后去重建一个本来就对的索引）。
    """
    harness = _eval_harness() if harness_name == "v1" else _eval_harness_v2()
    harness.assert_index_format(indexed, "bigram")

    captured = capsys.readouterr()
    assert "!!" not in captured.out + captured.err, (
        f"{harness_name} 检测器对生产建的索引报了不一致：{captured.out}{captured.err}"
    )
