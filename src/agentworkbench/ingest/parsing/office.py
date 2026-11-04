"""Office 抽取：Word（.docx）与 Excel（.xlsx），归一化成「段」。

输出契约与 PDF 一致：`list[str]`，第 i 个元素（1-based）就是引用里的"第 i 页"。
切块、索引、检索、引用四处一行不改——它们只认识「段」。

- Word：**整篇一段**。`python-docx` 不提供页码，假装按页切会给出用户翻不到的数字。
- Excel：**每个工作表一段**，按 sheet 顺序编号，段首带 sheet 名。

两个最容易写错的地方，判据（V-14 / V-15）专门守着：

1. **段落与表格必须按文档流顺序。** `document.paragraphs` 与 `document.tables`
   是两套视图，各读一遍会把所有表格搬到全文末尾——SOP 的参数表插在步骤中间，
   搬走之后表格上下的句子连成另一句话，切块与检索看不出任何异常，只是答案引错。
   所以这里遍历 `body` 按 `w:p` / `w:tbl` 分派，不用那两个属性。
2. **合并单元格要填到它覆盖的每一行。** openpyxl 只在左上角给值，其余是 `None`；
   不填的话"焊接"下面几行的工序列是空的，检索"焊接的参数"会漏掉它们。
   填充只对 `merged_cells.ranges` 覆盖的范围做——**盲目向下填充**会把"合计"行
   填成上一行的值，那比不填更错。

来源：C# 版未实现 Office（`功能需求.md` §3.3 的边界）；本文件是 Python 侧新增
服务于：REQ-05 / 06（F2）；判据 V-14 ~ V-18（`pyproject.toml`）。
"""

from __future__ import annotations

from io import BytesIO
from typing import Final
from zipfile import BadZipFile

from docx import Document as load_docx
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph
from openpyxl import load_workbook
from openpyxl.worksheet.worksheet import Worksheet

from agentworkbench.core.documents import DocumentErrorCode
from agentworkbench.ingest.parsing.errors import ParseError

MAX_ROWS_PER_SHEET: Final[int] = 2000
"""单个工作表进入正文的行数上限。

上限防的不是"文件大"，是**一个 sheet 把解析进程拖垮、连带同一进程里正在处理的
其它文档一起失败**。超过的行不读，但在正文里说出来——静默截断会让用户以为
整张表都进去了，而答案是按前两千行给的。

内存的已知限制：openpyxl 的合并单元格信息只在普通（非 `read_only`）模式可用，
普通模式解析是全量进内存。十万行的检验记录约几百 MB，可接受；百万行是重开条件
（届时 read_only + 自行解析 worksheet XML 里的 `<mergeCells>`），不在此处赌。
"""

# OLE 复合文档魔数。加密的 docx/xlsx 是 OLE 容器而不是 zip——python-docx /
# openpyxl 打开它们只会报"不是合法压缩包"，归成 corrupt 的话用户会去"重新导出"，
# 而正确的动作是解除密码。一行魔数判断换正确的用户动作，划算。
# 旧版 .doc / .xls 也是这个魔数，但它们在上传白名单就被拒了，到不了这里。
_OLE_MAGIC = b"\xd0\xcf\x11\xe0"


def _reject_ole(data: bytes) -> None:
    if data[:4] == _OLE_MAGIC:
        raise ParseError(
            DocumentErrorCode.ENCRYPTED,
            "这份文件有密码保护（或来自受保护的视图），请解除后重新上传。",
        )


def parse_docx(data: bytes) -> list[str]:
    """Word → 一段。段落与表格按文档流顺序，表格行内用 ` | ` 连接。"""
    _reject_ole(data)
    try:
        doc = load_docx(BytesIO(data))
    except (BadZipFile, ValueError, KeyError) as exc:
        # PackageNotFoundError 是 BadZipFile 的子类口径（python-docx 对非 zip
        # 与"zip 但不是 OOXML"都走这一族）。三者都是"文件不是它自称的格式"。
        raise ParseError(
            DocumentErrorCode.CORRUPT, "不是有效的 Word 文件，请从原始来源重新导出。"
        ) from exc

    lines: list[str] = []
    for child in doc.element.body.iterchildren():
        if child.tag == qn("w:p"):
            text = Paragraph(child, doc).text.strip()
            if text:
                lines.append(text)
        elif child.tag == qn("w:tbl"):
            lines.extend(_table_lines(Table(child, doc)))
    text = "\n".join(lines)
    if not text.strip():
        raise ParseError(
            DocumentErrorCode.NO_TEXT_LAYER,
            "这份 Word 里没有可读的文字——整篇可能是截图，需要先转成文字版。",
        )
    return [text]


def _table_lines(table: Table) -> list[str]:
    """一张表 → 行文本。横向合并的单元格只取一次。

    `row.cells` 对横向合并会返回**同一个 cell 对象多次**（gridSpan 补位），
    不去重的话"参数 | 350A"会变成"参数 | 参数 | 350A"——检索不受影响，
    但切块的字符预算被重复内容吃掉，重叠窗里也全是它。
    """
    out: list[str] = []
    for row in table.rows:
        seen: list[object] = []
        values: list[str] = []
        for cell in row.cells:
            if cell._tc in seen:
                continue
            seen.append(cell._tc)
            value = cell.text.strip()
            if value:
                values.append(value)
        line = " | ".join(values)
        if line:
            out.append(line)
    return out


def parse_xlsx(data: bytes, *, max_rows: int = MAX_ROWS_PER_SHEET) -> list[str]:
    """Excel → 每个工作表一段。合并区域填进每一行；超限的行不读、但要说明。"""
    _reject_ole(data)
    try:
        workbook = load_workbook(BytesIO(data), data_only=True)
    except (BadZipFile, ValueError, KeyError) as exc:
        raise ParseError(
            DocumentErrorCode.CORRUPT, "不是有效的 Excel 文件，请从原始来源重新导出。"
        ) from exc

    pages: list[str] = []
    try:
        for sheet in workbook.worksheets:
            pages.append(_sheet_text(sheet, max_rows=max_rows))
    finally:
        workbook.close()

    if not any(page.strip() for page in pages):
        raise ParseError(DocumentErrorCode.NO_TEXT_LAYER, "这份 Excel 里没有可读的单元格。")
    return pages


def _sheet_text(sheet: Worksheet, *, max_rows: int) -> str:
    """一个工作表 → 一段。标题行、合并填充、行上限都在这里。"""
    fills = _merged_fills(sheet)
    total = sheet.max_row or 0
    lines: list[str] = []
    for row_idx, row in enumerate(
        sheet.iter_rows(min_row=1, max_row=min(total, max_rows)), start=1
    ):
        values: list[str] = []
        for col_idx, cell in enumerate(row, start=1):
            # 索引用 enumerate 而不是 cell.row / cell.column：
            # openpyxl 的 stub 把它们标成 int | None，而 iter_rows 给出的必然有值
            raw: object = fills.get((row_idx, col_idx))
            if raw is None:
                raw = cell.value
            values.append("" if raw is None else str(raw).strip())
        line = " | ".join(values).strip(" |")
        if line:
            lines.append(line)
    if total > max_rows:
        lines.append(f"（截断：本表共 {total} 行，只读了前 {max_rows} 行）")
    return f"## 工作表：{sheet.title}\n" + "\n".join(lines)


def _merged_fills(sheet: Worksheet) -> dict[tuple[int, int], str]:
    """合并区域 → 每个被覆盖坐标的值。

    只填 `merged_cells.ranges` 声明过的范围。**不做"列内向下填充"**——
    那会把表尾"合计"行的空单元格填成上一行的值，而"合计"恰恰是最不该
    被归属到某个工序的行。
    """
    fills: dict[tuple[int, int], str] = {}
    for merged in sheet.merged_cells.ranges:
        min_col, min_row, max_col, max_row = merged.bounds
        top = sheet.cell(min_row, min_col).value
        if top is None:
            continue
        text = str(top).strip()
        for row in range(min_row, max_row + 1):
            for col in range(min_col, max_col + 1):
                fills[(row, col)] = text
    return fills
