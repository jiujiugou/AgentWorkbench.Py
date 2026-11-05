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

import re
from io import BytesIO
from typing import Final
from zipfile import BadZipFile, ZipFile

from docx import Document as load_docx
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph
from openpyxl import load_workbook
from openpyxl.utils import range_boundaries
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

MAX_COLS_PER_SHEET: Final[int] = 200
"""单个工作表进入正文的列数上限。

与行上限同一个理由，但 D9 才发现它缺着：`iter_rows` 不传 `max_col` 时会一直走到
`sheet.max_column`，而那个值由文件自己声明——一个把 `max_column` 写成 16384 的
文件就能让解析线程扫 2000×16384 个空单元格。Excel 的宽表很少超过 200 列，
超过的部分对"把表读成正文"没有价值。
"""

_MERGE_AREA_LIMIT: Final[int] = 200_000
"""整份工作簿的合并区域总面积上限（单元格数）。超过就**退回流式读取**。

为什么这个阈值必须在 `load_workbook` **之前**判：openpyxl 在普通（非 `read_only`）
模式下会把合并区域物化成 `MergedCell` 对象——D9 实测一个 51KB 的 xlsx
（`A2001:Z20000`，52 万格）光 `load_workbook` 就 12.5 秒，而那是**加载期**，
`_merged_fills` 里怎么裁剪都碰不到。改完之后走 `read_only=True`：不物化合并格，
正文照常读出（少掉合并填充，段尾会写明），解析时间回到 0.3 秒量级。
"""

_MERGE_REF: Final[re.Pattern[bytes]] = re.compile(
    rb'<mergeCell[^>]*ref="([A-Z]+[0-9]+:[A-Z]+[0-9]+)"'
)
"""合并区域展开的**总**单元格预算。

`MAX_ROWS` / `MAX_COLS` 只截得住"读多少行列"，截不住"填多少坐标"：合并区域
`A2001:Z20000` 落在读窗口之外，却要在 `_merged_fills` 里展开成 36 万个格子。
D9 实测一个 51KB 的 xlsx（`A2001:Z20000`）让解析烧 12.6 秒，按线性外推
`A2001:Z200000` 是 3~4 分钟、`A1:XFD1048576` 是 170 亿格——文件仍然只有几百 KB。

预算是兜底而不是常规路径：正常表格的合并区域是几十个格子，永远碰不到它。
超过就停下，少填的格子退回单元格自己的值（正文里最多丢掉几处合并填充，
而"解析线程被一个 51KB 文件占住几分钟"是可用性问题）。
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


def _merged_area(data: bytes) -> int:
    """整份工作簿里合并区域的总格数。**只读 zip 里的声明，不加载工作簿。**

    解析失败一律当作 0：这个函数是"要不要走流式读取"的启发式，判错的代价只是
    退回普通路径（也就是 D9 之前的行为），不该让一个奇怪的文件在这里炸掉。
    """
    total = 0
    try:
        with ZipFile(BytesIO(data)) as archive:
            for name in archive.namelist():
                if not (name.startswith("xl/worksheets/") and name.endswith(".xml")):
                    continue
                for match in _MERGE_REF.finditer(archive.read(name)):
                    try:
                        bounds = range_boundaries(match.group(1).decode("ascii"))
                    except ValueError:  # pragma: no cover - 非法 ref 交给 openpyxl 去报
                        continue
                    # 类型上是 `int | None` 的四元组（绝对引用与整列引用的差别），
                    # 拿不到具体边界就跳过——这个数只是"要不要走流式"的启发式。
                    if bounds is None or any(item is None for item in bounds):
                        continue
                    rows = int(bounds[3] or 0) - int(bounds[1] or 0) + 1
                    cols = int(bounds[2] or 0) - int(bounds[0] or 0) + 1
                    total += max(0, rows) * max(0, cols)
    except (BadZipFile, KeyError, OSError, ValueError):
        return 0
    return total


def parse_xlsx(data: bytes, *, max_rows: int = MAX_ROWS_PER_SHEET) -> list[str]:
    """Excel → 每个工作表一段。合并区域填进每一行；超限的行不读、但要说明。"""
    _reject_ole(data)
    # 合并区域大到病态时走流式读取：openpyxl 在普通模式下会把每一格合并区域物化成
    # `MergedCell` 对象，52 万格就要 12 秒（D9 实测）。判据必须放在**加载之前**——
    # 那 12 秒全在 `load_workbook` 里，`_merged_fills` 里的裁剪一行也帮不上。
    streaming = _merged_area(data) > _MERGE_AREA_LIMIT
    try:
        workbook = load_workbook(BytesIO(data), data_only=True, read_only=streaming)
    except (BadZipFile, ValueError, KeyError) as exc:
        raise ParseError(
            DocumentErrorCode.CORRUPT, "不是有效的 Excel 文件，请从原始来源重新导出。"
        ) from exc

    pages: list[str] = []
    try:
        for sheet in workbook.worksheets:
            pages.append(_sheet_text(sheet, max_rows=max_rows, merged=not streaming))
    finally:
        workbook.close()

    if not any(page.strip() for page in pages):
        raise ParseError(DocumentErrorCode.NO_TEXT_LAYER, "这份 Excel 里没有可读的单元格。")
    return pages


def _sheet_text(
    sheet: Worksheet,
    *,
    max_rows: int,
    max_cols: int = MAX_COLS_PER_SHEET,
    merged: bool = True,
) -> str:
    """一个工作表 → 一段。标题行、合并填充、行/列上限都在这里。

    `merged=False` 表示这份工作簿是**流式读**的（合并区域大到病态）：`read_only`
    模式下没有 `merged_cells`，于是不做填充，并在段尾写明——静默少掉填充会让
    用户以为表格本来就这样。
    """
    fills = _merged_fills(sheet, max_rows=max_rows, max_cols=max_cols) if merged else {}
    total = sheet.max_row or 0
    total_cols = sheet.max_column or 0
    lines: list[str] = []
    for row_idx, row in enumerate(
        sheet.iter_rows(
            min_row=1, max_row=min(total, max_rows), max_col=min(total_cols, max_cols) or 1
        ),
        start=1,
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
    if total_cols > max_cols:
        lines.append(f"（截断：本表共 {total_cols} 列，只读了前 {max_cols} 列）")
    if not merged:
        lines.append("（本表合并单元格过多，已跳过合并填充，取值以左上角为准）")
    return f"## 工作表：{sheet.title}\n" + "\n".join(lines)


def _merged_fills(
    sheet: Worksheet,
    *,
    max_rows: int = MAX_ROWS_PER_SHEET,
    max_cols: int = MAX_COLS_PER_SHEET,
    budget: int = _MERGE_AREA_LIMIT,
) -> dict[tuple[int, int], str]:
    """合并区域 → 每个被覆盖坐标的值。

    只填 `merged_cells.ranges` 声明过的范围。**不做"列内向下填充"**——
    那会把表尾"合计"行的空单元格填成上一行的值，而"合计"恰恰是最不该
    被归属到某个工序的行。

    **裁剪到读窗口内 + 总预算**：只填会被读到的格子。合并区域写到第 20000 行
    而正文只读前 2000 行时，把那些格子全展开是纯浪费——而浪费的是解析线程的
    CPU，且文件大小完全不受 `max_upload_bytes` 约束（一个 51KB 的 xlsx 就够）。
    """
    fills: dict[tuple[int, int], str] = {}
    spent = 0
    for merged in sheet.merged_cells.ranges:
        first_col, first_row, last_col, last_row = merged.bounds
        top = sheet.cell(first_row, first_col).value
        if top is None:
            continue
        # 裁剪的是**填充范围**，不是左上角取值的位置：合并区的值永远在它自己的
        # 左上角，裁剪之后仍要从那里读。
        row_from, row_to = max(1, first_row), min(last_row, max_rows)
        col_from, col_to = max(1, first_col), min(last_col, max_cols)
        if row_from > row_to or col_from > col_to:
            continue
        area = (row_to - row_from + 1) * (col_to - col_from + 1)
        if spent + area > budget:
            break
        spent += area
        text = str(top).strip()
        for row in range(row_from, row_to + 1):
            for col in range(col_from, col_to + 1):
                fills[(row, col)] = text
    return fills
