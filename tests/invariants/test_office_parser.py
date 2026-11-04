# ruff: noqa: S106 —— 本文件的 secret_key 全是测试值，不连任何真实账号。
"""Office 格式（Word / Excel）的解析不变量：段划分、顺序、合并单元格、失败归类、上限。

**这份文件先于实现存在**（TDD 的那一半）。它钉的是接口与判据，不是实现细节：

- 三种格式归一化成 `list[str]`，第 i 个元素（1-based）就是引用里的"第 i 页"
- 分派按**后缀**，不靠嗅探魔数：上传时后缀已经被白名单校验过，是可信号
- 失败仍走 `DocumentErrorCode`，不新开一套码

为什么不嗅探魔数：docx 与 xlsx 都是 zip，真正的判别在 `[Content_Types].xml` 里。
为了区分这两者去解一层 zip，得到的结论与后缀一致，而代价是多一条"后缀与内容不符"
时的第二套归因路径——那正是 `no_text_layer` 与 `corrupt` 当初分开要避免的东西。

**改坏之后用户看到什么**（这是本文件的验收物，不是注释——写不出失效形态的断言
本来就不该存在）：

- 段落与表格分开读（先 paragraphs 再 tables）：SOP 的参数表被搬到文末，
  表格上下的句子连成另一句话。答案引错，而切块与检索看不出任何异常
- 不填合并单元格：问"焊接的参数"漏掉两行；那两行在 Excel 里明明和第一行是一组
- 损坏归成 `extract_error` 而不是 `corrupt`：用户被引导去"重试"，
  而重试一万次也是同一个错
- 未知后缀回落到 PDF：传 `.txt` 被告知"文件损坏"，实际是"格式不支持"——
  归因错一层，用户按错的方向修文件
- 大表不设上限：一份十万行的检验记录把解析进程撑死，
  连带同一进程里正在处理的其它文档一起失败

服务于：REQ-05 / 06（F2）；判据 V-13 ~ V-18（`pyproject.toml`）。
"""

from __future__ import annotations

from io import BytesIO

import pytest
from docx import Document
from openpyxl import Workbook

from agentworkbench.core.documents import DocumentErrorCode
from agentworkbench.ingest.parsing.dispatch import UnsupportedFormat, parse_document
from agentworkbench.ingest.parsing.errors import ParseError
from agentworkbench.ingest.parsing.office import parse_xlsx
from agentworkbench.ingest.upload import (
    DEFAULT_ALLOWED_SUFFIXES,
    UploadRejectCode,
    judge_upload,
)

# ── 造样本：用库现造，不入库二进制 ──
# 二进制 fixture 会随库升级悄悄变、diff 里看不出内容，而且"这个文件为什么长这样"
# 要另外写一份说明。现造的样本自带说明。


def _docx(blocks: list[object]) -> bytes:
    """按文档流顺序造一份 docx。`blocks` 里 str 是段落，list 是表格（行 × 列）。

    表格块是**三层**：`[[["a","b"],["c","d"]]]` = 一张 2×2 表。第二层若写成
    `["a","b"]`，rows 会迭代出两个**字符**——`"a".__len__` 当列数、
    每个字符当一行，产出一张鬼 table。这里用断言把它挡在构造期，
    而不是等解析结果诡异地错。
    """
    doc = Document()
    for block in blocks:
        if isinstance(block, str):
            doc.add_paragraph(block)
        else:
            rows = block
            assert isinstance(rows, list) and rows
            assert all(isinstance(row, list) for row in rows), (
                "表格块的每行必须是 list，传字符串会被逐字符迭代成一张鬼表"
            )
            table = doc.add_table(rows=len(rows), cols=len(rows[0]))
            for r, row in enumerate(rows):
                for c, value in enumerate(row):
                    table.cell(r, c).text = str(value)
    buffer = BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


def _xlsx(sheets: list[tuple[str, list[list[object]]]]) -> bytes:
    """造一份多 sheet 的 xlsx。"""
    workbook = Workbook()
    active = workbook.active
    assert active is not None
    workbook.remove(active)
    for name, rows in sheets:
        sheet = workbook.create_sheet(name)
        for row in rows:
            sheet.append(row)
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


# ── V-13：上传收三类，拒旧二进制格式 ──


@pytest.mark.parametrize("suffix", [".pdf", ".docx", ".xlsx"])
def test_the_three_formats_are_accepted(suffix: str) -> None:
    """白名单是三类的**唯一来源**：判据里的"三类"从常量取，不从测试里另写一份。

    另写一份的话，白名单漏加 `.xlsx` 时这条照样绿——而用户传 Excel 就被拒。
    """
    assert suffix in DEFAULT_ALLOWED_SUFFIXES
    accepted = judge_upload(filename=f"手册{suffix}", size_bytes=1024, max_bytes=1024 * 1024)
    assert accepted is None


@pytest.mark.parametrize("suffix", [".doc", ".xls", ".xlsm", ".txt", ".png"])
def test_legacy_and_foreign_formats_are_refused(suffix: str) -> None:
    """`.doc` / `.xls` 是二进制复合格式，两条库都不读；`.xlsm` 含宏，不读也不该收。

    `.xlsm` 特别要挡：`openpyxl` 能打开它（忽略宏），于是"收了但静默丢宏"——
    用户以为公式和宏都在，实际只拿到了值。
    """
    refused = judge_upload(filename=f"台账{suffix}", size_bytes=1024, max_bytes=1024 * 1024)
    assert refused is UploadRejectCode.FILE_TYPE_UNSUPPORTED


def test_settings_default_matches_the_whitelist() -> None:
    """生产真源是 `Settings.allowed_suffixes`（路由读 `suffix_set`），常量只是引用点。

    两处曾各改各的：常量加了 Office 三类，配置还是 `.pdf`——HTTP 上传仍被拒，
    而本文件全部测试绿（`judge_upload` 的默认参数走常量）。这条锁住默认值一致；
    运维显式收紧配置仍是合法操作，锁的是**默认值**不是运行值。
    """
    from agentworkbench.config import Settings

    assert Settings(secret_key="test-key").suffix_set == DEFAULT_ALLOWED_SUFFIXES


# ── V-14：Word 的段落与表格按文档流顺序，且都不丢 ──


def test_word_keeps_paragraph_table_paragraph_order() -> None:
    """段落 → 表格 → 段落 的顺序必须保持。

    这是本文件最要紧的一条。`python-docx` 的 `document.paragraphs` 与
    `document.tables` 是**两套视图**，各自按自己的顺序给：先读 paragraphs 再读
    tables 的写法会把所有表格搬到全文末尾。SOP 里"参数表"插在步骤中间，
    搬走之后表格上下的句子会连在一起，抽出的话变成另一句话——而切块和检索
    看不出任何异常，只是答案引错。
    """
    data = _docx(["开工前检查气压", [["电流", "120A"]], "收工后记录读数"])
    pages = parse_document(data, ".docx")

    text = "\n".join(pages)
    assert text.index("开工前检查气压") < text.index("120A") < text.index("收工后记录读数")


def test_word_tables_are_extracted_at_all() -> None:
    """只读 `paragraphs` 的实现必须红：制造业的工艺参数大量在表格里。"""
    data = _docx([[["焊接电流", "350A"], ["预热温度", "80℃"]]])
    pages = parse_document(data, ".docx")

    text = "\n".join(pages)
    assert "350A" in text
    assert "80℃" in text


def test_word_is_a_single_segment() -> None:
    """Word 整篇一段：`python-docx` 不提供页码，假装按页切会给出用户翻不到的数字。

    段数进 `documents.page_count`，也决定引用显示"第几页"。这条断言把
    "以后有人想按标题分段"变成一个要让测试变红的决定，而不是顺手改掉。
    """
    pages = parse_document(_docx(["一", "二", "三"]), ".docx")
    assert len(pages) == 1


# ── V-15：Excel 每个工作表一段，合并单元格不漏 ──


def test_each_sheet_is_its_own_segment() -> None:
    """两个 sheet → 两段。不分段的实现会把两张表的行糊在一起，
    于是"汇总表里的合计"可能来自"台账表里的某行"。"""
    data = _xlsx(
        [
            ("台账", [["工序", "参数"], ["焊接", "350"]]),
            ("汇总", [["合计", "12"]]),
        ]
    )
    pages = parse_document(data, ".xlsx")

    assert len(pages) == 2
    assert "台账" in pages[0]
    assert "汇总" in pages[1]


def test_merged_cells_are_filled_into_every_row() -> None:
    """合并区域的每一行都要有那个值。

    openpyxl 读合并区域时只有左上角有值，其余是 `None`。不填的话，
    "焊接"下面几行的"工序"列是空的——检索"焊接的参数是多少"会漏掉那几行，
    而它们在 Excel 里看着明明是同一组。这是 Excel 处理最典型的一个坑。
    """
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "台账"
    sheet.append(["工序", "参数"])
    sheet.append(["焊接", 350])
    sheet.append([None, 360])
    sheet.append([None, 370])
    sheet.merge_cells("A2:A4")
    buffer = BytesIO()
    workbook.save(buffer)

    pages = parse_document(buffer.getvalue(), ".xlsx")
    lines = [line for line in pages[0].splitlines() if "焊接" in line]

    assert len(lines) == 3, f"合并值只出现在 {len(lines)} 行，应该每行都有"
    # 更强的一侧：每行除了"焊接"还要带上自己那行的参数，
    # 否则"填了但填错行"也能过。
    for expected in ("350", "360", "370"):
        assert any(expected in line for line in lines), f"第 {expected} 行丢了参数"


# ── V-16：没有文字层的 Office 文件是 no_text_layer，不是 corrupt ──


def test_office_without_text_is_no_text_layer() -> None:
    """空文档（现场常见的是"整篇由截图组成"）必须归到 `no_text_layer`。

    这一个码决定用户看到的话：`no_text_layer` 是"这是扫描件，需要识别"，
    `corrupt` 是"文件坏了，请重新导出"。两句话对应两个不同的用户动作，
    混成一个之后，统计也分不出"这个月有多少件是扫描件"——而那是要不要做 OCR
    的唯一依据。
    """
    empty = Document()
    buffer = BytesIO()
    empty.save(buffer)

    with pytest.raises(ParseError) as caught:
        parse_document(buffer.getvalue(), ".docx")

    assert caught.value.code is DocumentErrorCode.NO_TEXT_LAYER


# ── V-17：损坏是 corrupt，未知后缀是分派错误，不许回落到 PDF ──


def test_a_broken_office_file_is_corrupt() -> None:
    """不是合法 zip 的 .docx → `corrupt`，不是 `extract_error`。

    `extract_error` 是"读到了但某一页失败"，`corrupt` 是"这个文件根本不是
    那个格式"。前者重试可能成功，后者一定失败——用户动作不同。
    """
    with pytest.raises(ParseError) as caught:
        parse_document(b"this is not a zip file at all", ".docx")

    assert caught.value.code is DocumentErrorCode.CORRUPT


def test_an_unknown_suffix_is_refused_not_parsed_as_pdf() -> None:
    """未注册的后缀必须抛 `UnsupportedFormat`，不许回落到 PDF。

    回落的版本会把 `.txt` 报成 `corrupt`——归因错了一层，而且错得看不出来：
    用户看到"文件坏了"，实际是"这个格式我们不支持"。
    """
    with pytest.raises(UnsupportedFormat):
        parse_document(b"%PDF-1.7 fake", ".txt")


def test_pdf_still_goes_to_pypdf() -> None:
    """正控：分派没有把 `.pdf` 也一起拒掉。

    缺这一条时，"把所有后缀都抛 UnsupportedFormat"的实现能过上面那条。
    """
    with pytest.raises(ParseError) as caught:
        parse_document(b"%PDF-1.7 truncated", ".pdf")

    # 假 PDF 被 pypdf 判成损坏，而不是被判成"没见过这个后缀"
    assert caught.value.code is DocumentErrorCode.CORRUPT


# ── V-18：大表有上限，不许把内存吃光 ──


def test_rows_are_capped() -> None:
    """超过上限的行不读。

    上限的作用不是"节省流量"，是防止一个 10 万行的检验记录把解析进程撑死——
    那会连带同一进程里正在处理的其它文档一起失败。截断要**说出来**，
    静默截断会让用户以为整张表都进去了。
    """
    rows: list[list[object]] = [["列一", "列二"]]
    for i in range(20):
        rows.append([f"行{i}", i])
    data = _xlsx([("大表", rows)])

    # max_rows 的口径是**物理行**（含表头）：6 = 表头 + 行0..行4
    pages = parse_xlsx(data, max_rows=6)
    body = pages[0]

    assert "行4" in body
    assert "行5" not in body, "上限之外的行被读进来了"
    assert "截断" in body, "截断了却没说，用户会以为整张表都在"
