"""Read and write simple .xlsx workbooks with the standard library only.

An .xlsx file is a zip of XML files. Writing those files directly means the
evals need no spreadsheet package installed. The subset supported is what the
eval workbooks use: text, numbers, formulas, a handful of styles, frozen
panes, filters, Y/N dropdowns and green/red highlighting.
"""

from __future__ import annotations

import math
import re
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

# --------------------------------------------------------------------------- #
# Styles (indexes into STYLES_XML's cellXfs)
# --------------------------------------------------------------------------- #

DEFAULT, HEADER, BODY, TITLE, LABEL, PLAIN, HEADING, TEXT, NOTE, PERCENT, NUMBER, INPUT = range(12)
# Conditional-format looks (indexes into dxfs)
DXF_HIGHLIGHT, DXF_GOOD, DXF_BAD = range(3)

STYLES_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<numFmts count="1"><numFmt numFmtId="164" formatCode="0.0"/></numFmts>
<fonts count="5">
<font><sz val="11"/><name val="Calibri"/><family val="2"/></font>
<font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/><family val="2"/></font>
<font><b/><sz val="16"/><color rgb="FF1C7350"/><name val="Calibri"/><family val="2"/></font>
<font><b/><sz val="11"/><name val="Calibri"/><family val="2"/></font>
<font><i/><sz val="11"/><color rgb="FF595959"/><name val="Calibri"/><family val="2"/></font>
</fonts>
<fills count="5">
<fill><patternFill patternType="none"/></fill>
<fill><patternFill patternType="gray125"/></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FF1C7350"/><bgColor indexed="64"/></patternFill></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFEAF4EE"/><bgColor indexed="64"/></patternFill></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFFFFBE6"/><bgColor indexed="64"/></patternFill></fill>
</fills>
<borders count="2">
<border><left/><right/><top/><bottom/><diagonal/></border>
<border><left style="thin"><color rgb="FFD0D7D3"/></left><right style="thin"><color rgb="FFD0D7D3"/></right><top style="thin"><color rgb="FFD0D7D3"/></top><bottom style="thin"><color rgb="FFD0D7D3"/></bottom><diagonal/></border>
</borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="12">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>
<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>
<xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1"/>
<xf numFmtId="0" fontId="3" fillId="3" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>
<xf numFmtId="0" fontId="3" fillId="0" borderId="0" xfId="0" applyFont="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>
<xf numFmtId="49" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>
<xf numFmtId="0" fontId="4" fillId="0" borderId="0" xfId="0" applyFont="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>
<xf numFmtId="9" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="top"/></xf>
<xf numFmtId="164" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="top"/></xf>
<xf numFmtId="0" fontId="3" fillId="4" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="top"/></xf>
</cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
<dxfs count="3">
<dxf><fill><patternFill patternType="solid"><bgColor rgb="FFFFF4CC"/></patternFill></fill></dxf>
<dxf><font><b/><color rgb="FF1C7350"/></font><fill><patternFill patternType="solid"><bgColor rgb="FFDFF3E6"/></patternFill></fill></dxf>
<dxf><font><b/><color rgb="FFB42318"/></font><fill><patternFill patternType="solid"><bgColor rgb="FFFDE4E1"/></patternFill></fill></dxf>
</dxfs>
<tableStyles count="0" defaultTableStyle="TableStyleMedium2" defaultPivotStyle="PivotStyleLight16"/>
</styleSheet>"""


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Formula:
    """A cell formula, without the leading '='. Excel works out the value."""

    text: str


@dataclass
class Sheet:
    """One worksheet. ``rows`` holds (value, style) pairs; values may be str,
    int, float, :class:`Formula` or None."""

    name: str
    rows: list[list[tuple[Any, int]]]
    widths: list[float]
    freeze: tuple[int, int] | None = None  # (columns, rows) kept in view
    autofilter: bool = False
    conditional: list[str] = field(default_factory=list)
    validations: list[str] = field(default_factory=list)


def col(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    letters, index = "", index + 1
    while index:
        index, rest = divmod(index - 1, 26)
        letters = chr(65 + rest) + letters
    return letters


def yes_no_dropdown(sqref: str) -> str:
    return (f'<dataValidation type="list" allowBlank="1" showErrorMessage="1" sqref="{sqref}">'
            '<formula1>"Y,N"</formula1></dataValidation>')


def highlight_equal(sqref: str, value: str, dxf: int, priority: int) -> str:
    return (f'<conditionalFormatting sqref="{sqref}"><cfRule type="cellIs" dxfId="{dxf}" '
            f'priority="{priority}" operator="equal"><formula>"{escape(value)}"</formula></cfRule>'
            "</conditionalFormatting>")


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #


def _cell(ref: str, value: Any, style: int) -> str:
    if value is None or value == "":
        return f'<c r="{ref}" s="{style}"/>'
    if isinstance(value, Formula):
        return f'<c r="{ref}" s="{style}"><f>{escape(value.text)}</f></c>'
    if isinstance(value, bool):
        value = "Y" if value else "N"
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return f'<c r="{ref}" s="{style}"/>'
        return f'<c r="{ref}" s="{style}"><v>{value}</v></c>'
    return (f'<c r="{ref}" t="inlineStr" s="{style}"><is><t xml:space="preserve">'
            f"{escape(str(value))}</t></is></c>")


_WRAPPING = {HEADER, BODY, LABEL, PLAIN, HEADING, TEXT, NOTE}


def _height(row: list[tuple[Any, int]], widths: list[float]) -> float:
    """A rough row height for wrapped text; Excel refits it when finishing.

    Only wrapping cells count: unwrapped text spills across empty cells beside
    it and stays on one line.
    """
    lines = 1
    for (value, style), width in zip(row, widths, strict=False):
        if value is None or isinstance(value, (Formula, int, float)) or style not in _WRAPPING:
            continue
        parts = str(value).split("\n")
        lines = max(lines, sum(max(1, math.ceil(len(p) / max(width - 1, 1))) for p in parts))
    return min(15 * lines + 3, 409)


def _sheet_xml(sheet: Sheet, selected: bool) -> str:
    cols = "".join(
        f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>' for i, w in enumerate(sheet.widths)
    )
    body = []
    for r, row in enumerate(sheet.rows, start=1):
        cells = "".join(_cell(f"{col(i)}{r}", v, s) for i, (v, s) in enumerate(row))
        body.append(f'<row r="{r}" ht="{_height(row, sheet.widths)}" customHeight="1">{cells}</row>')
    if sheet.freeze:
        xs, ys = sheet.freeze
        top_left = f"{col(xs)}{ys + 1}"
        pane = "bottomRight" if xs and ys else ("topRight" if xs else "bottomLeft")
        view = (f'<pane{f" xSplit=\"{xs}\"" if xs else ""}{f" ySplit=\"{ys}\"" if ys else ""} '
                f'topLeftCell="{top_left}" activePane="{pane}" state="frozen"/>'
                f'<selection pane="{pane}" activeCell="{top_left}" sqref="{top_left}"/>')
    else:
        view = '<selection activeCell="A1" sqref="A1"/>'
    last = f"{col(max(len(sheet.widths), 1) - 1)}{max(len(sheet.rows), 1)}"
    extra = ""
    if sheet.autofilter:
        extra += f'<autoFilter ref="A1:{last}"/>'
    extra += "".join(sheet.conditional)
    if sheet.validations:
        extra += f'<dataValidations count="{len(sheet.validations)}">{"".join(sheet.validations)}</dataValidations>'
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<dimension ref="A1:{last}"/>'
        f'<sheetViews><sheetView workbookViewId="0"{" tabSelected=\"1\"" if selected else ""}>{view}'
        "</sheetView></sheetViews>"
        '<sheetFormatPr defaultRowHeight="15"/>'
        f"<cols>{cols}</cols><sheetData>{''.join(body)}</sheetData>{extra}"
        '<pageMargins left="0.5" right="0.5" top="0.75" bottom="0.75" header="0.3" footer="0.3"/>'
        "</worksheet>"
    )


def write_workbook(path: Path, sheets: list[Sheet]) -> None:
    """Write ``sheets`` to a new .xlsx at ``path``. Refuses to overwrite."""
    if path.exists():
        raise FileExistsError(f"{path} already exists")
    names = "".join(
        f'<sheet name="{escape(s.name)}" sheetId="{i}" r:id="rId{i}"/>' for i, s in enumerate(sheets, start=1)
    )
    filters = "".join(
        f"<definedName name=\"_xlnm._FilterDatabase\" localSheetId=\"{i}\" hidden=\"1\">"
        f"'{escape(s.name)}'!$A$1:${col(len(s.widths) - 1)}${len(s.rows)}</definedName>"
        for i, s in enumerate(sheets) if s.autofilter
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<bookViews><workbookView activeTab="0"/></bookViews><sheets>{names}</sheets>'
        + (f"<definedNames>{filters}</definedNames>" if filters else "")
        # Formulas are stored without values; this makes Excel calculate them on open.
        + '<calcPr calcId="191029" fullCalcOnLoad="1"/></workbook>'
    )
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(f'<Relationship Id="rId{i}" Type="{rel}/worksheet" Target="worksheets/sheet{i}.xml"/>'
                  for i in range(1, len(sheets) + 1))
        + f'<Relationship Id="rId{len(sheets) + 1}" Type="{rel}/styles" Target="styles.xml"/></Relationships>'
    )
    sheet_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"
    types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        + "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="{sheet_type}"/>'
                  for i in range(1, len(sheets) + 1))
        + '<Override PartName="/xl/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>'
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'<Relationship Id="rId1" Type="{rel}/officeDocument" Target="xl/workbook.xml"/></Relationships>'
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as package:
        package.writestr("[Content_Types].xml", types)
        package.writestr("_rels/.rels", root_rels)
        package.writestr("xl/workbook.xml", workbook)
        package.writestr("xl/_rels/workbook.xml.rels", rels)
        package.writestr("xl/styles.xml", STYLES_XML)
        for i, sheet in enumerate(sheets, start=1):
            package.writestr(f"xl/worksheets/sheet{i}.xml", _sheet_xml(sheet, selected=i == 1))


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #

_NS = {
    "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}


def _read_shared(path: Path) -> bytes:
    """Read a file's bytes, even while it is open in Excel.

    Excel holds an open workbook with access that Python's open() does not
    share, so open() fails with "Permission denied" although reading is
    harmless. On Windows the file is opened with full sharing instead. What is
    read is the last version Excel saved.
    """
    import sys

    if sys.platform != "win32":
        return path.read_bytes()
    import ctypes
    import msvcrt
    import os
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    generic_read, share_all, open_existing, normal = 0x80000000, 0x7, 3, 0x80
    handle = kernel32.CreateFileW(str(path), generic_read, share_all, None, open_existing, normal, None)
    if handle in (None, ctypes.c_void_p(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    with os.fdopen(msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY), "rb") as file:
        return file.read()


def read_sheet(path: Path, name: str) -> list[dict[str, str]]:
    """Read one sheet as a list of dicts keyed by the header row.

    Works on files saved by Excel (shared strings) and by this module (inline
    strings), including a workbook that is open in Excel right now. Every value
    comes back as text; empty rows are skipped.
    """
    import io

    with zipfile.ZipFile(io.BytesIO(_read_shared(Path(path)))) as package:
        strings: list[str] = []
        if "xl/sharedStrings.xml" in package.namelist():
            for item in ET.fromstring(package.read("xl/sharedStrings.xml")).findall("m:si", _NS):
                strings.append("".join(t.text or "" for t in item.iter(f"{{{_NS['m']}}}t")))
        book = ET.fromstring(package.read("xl/workbook.xml"))
        rels = {r.get("Id"): r.get("Target") for r in ET.fromstring(package.read("xl/_rels/workbook.xml.rels"))}
        targets = {s.get("name"): rels[s.get(f"{{{_NS['r']}}}id")] for s in book.find("m:sheets", _NS)}
        if name not in targets:
            raise KeyError(f"no sheet called {name!r} (found {sorted(targets)})")
        target = targets[name].lstrip("/")
        target = target if target.startswith("xl/") else f"xl/{target}"
        data = ET.fromstring(package.read(target)).find("m:sheetData", _NS)

    def text(cell: ET.Element) -> str:
        kind = cell.get("t")
        if kind == "s":
            return strings[int(cell.find("m:v", _NS).text)]
        if kind == "inlineStr":
            return "".join(t.text or "" for t in cell.iter(f"{{{_NS['m']}}}t"))
        value = cell.find("m:v", _NS)
        return value.text if value is not None and value.text else ""

    grid: list[list[str]] = []
    for row in data:
        cells: dict[int, str] = {}
        for cell in row:
            letters = re.match(r"[A-Z]+", cell.get("r")).group()
            index = 0
            for ch in letters:
                index = index * 26 + ord(ch) - 64
            cells[index - 1] = text(cell)
        if any(value.strip() for value in cells.values()):
            grid.append([cells.get(i, "") for i in range(max(cells) + 1)])
    if not grid:
        return []
    header = [h.strip() for h in grid[0]]
    return [dict(zip(header, row + [""] * (len(header) - len(row)), strict=False)) for row in grid[1:]]
