"""
Build Word, Excel and PDF files from text the model writes (PLAN 4.27, writing).

``write_file`` calls ``build_document`` for a ``.docx`` / ``.xlsx`` / ``.pdf`` path, so no new tool or approval tier
exists: replacing a file still asks and backs up first. CSV needs nothing here (it is text).

The text conventions are the ones ``doc_readers`` prints, so a document read with ``read_file`` can be edited and
written back the same way:

* Word and PDF: Markdown. ``#``..``######`` headings, ``-``/``*`` bullets, ``1.`` numbered items, ``| a | b |`` table rows
  (first row is the header), blank line between paragraphs, ``**bold**`` and ``*italic*``.
* Excel: CSV rows (a tab-separated line works too); ``--- sheet: Name ---`` starts a sheet. ``=SUM(A1:A3)`` is a formula
  (Excel recalculates on open), numbers are numbers, ``2026-10-03`` is a date, ``TRUE``/``FALSE`` are booleans.

Word and Excel are written straight to OOXML with ``zipfile`` (no dependency). PDF uses ``reportlab``, imported on first use,
with a system TrueType font (Segoe UI / Arial on Windows, DejaVu / Liberation elsewhere) and the built-in Helvetica when none
is found. A character the font cannot draw is replaced by ``?`` and the result says so. Text that needs shaping (Devanagari,
Arabic) has glyphs in some fonts but is not laid out correctly: use Word for it.
"""
from __future__ import annotations

import csv
import io
import os
import re
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Optional
from xml.sax.saxutils import escape as _esc

BUILDABLE_EXTENSIONS = frozenset({".docx", ".xlsx", ".pdf"})
MAX_CONTENT_CHARS = 2_000_000
MAX_XLSX_ROWS = 100_000
MAX_XLSX_COLS = 200

_XML_BAD = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")


class BuildError(Exception):
    """A readable reason, returned to the model as is."""


def _x(text: str) -> str:
    """Escape for XML text and drop characters XML 1.0 cannot carry."""
    return _esc(_XML_BAD.sub("", text))


def _attr(text: str) -> str:
    return _x(text).replace('"', "&quot;")


# ---------------------------------------------------------------- Markdown-ish parsing (Word, PDF)
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")
_NUMBERED = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_RULE = re.compile(r"^\s*\|?(\s*:?-{2,}:?\s*\|)+\s*:?-*:?\s*$")
_INLINE = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*|\*(?=\S)(.+?)(?<=\S)\*")


def parse_blocks(text: str) -> list:
    """[('heading', level, text) | ('para', text) | ('bullet', text) | ('number', text) | ('table', [[cell]])]"""
    blocks: list = []
    para: list = []
    table: list = []

    def flush_para():
        if para:
            blocks.append(("para", " ".join(para)))
            para.clear()

    def flush_table():
        if table:
            blocks.append(("table", [list(r) for r in table]))
            table.clear()

    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.rstrip()
        if _TABLE_ROW.match(line):
            flush_para()
            if not _TABLE_RULE.match(line):
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                table.append(cells)
            continue
        flush_table()
        if not line.strip():
            flush_para()
            continue
        m = _HEADING.match(line)
        if m:
            flush_para()
            blocks.append(("heading", len(m.group(1)), m.group(2)))
            continue
        m = _BULLET.match(line)
        if m:
            flush_para()
            blocks.append(("bullet", m.group(1)))
            continue
        m = _NUMBERED.match(line)
        if m:
            flush_para()
            blocks.append(("number", m.group(1)))
            continue
        para.append(line.strip())
    flush_para()
    flush_table()
    return blocks


def _runs(text: str) -> list:
    """[(text, bold, italic)] from **bold** and *italic*."""
    out, pos = [], 0
    for m in _INLINE.finditer(text):
        if m.start() > pos:
            out.append((text[pos:m.start()], False, False))
        if m.group(1) is not None:
            out.append((m.group(1), True, False))
        else:
            out.append((m.group(2), False, True))
        pos = m.end()
    if pos < len(text):
        out.append((text[pos:], False, False))
    return out or [("", False, False)]


# ---------------------------------------------------------------- DOCX
_W_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
_CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_OD = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

_DOCX_STYLES = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles {_W_NS}>
<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:eastAsia="Calibri" w:cs="Calibri"/><w:sz w:val="22"/><w:szCs w:val="22"/><w:lang w:val="en-US"/></w:rPr></w:rPrDefault>
<w:pPrDefault><w:pPr><w:spacing w:after="120" w:line="264" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>
<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/></w:style>
{"".join(
    f'<w:style w:type="paragraph" w:styleId="Heading{n}"><w:name w:val="heading {n}"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/>'
    f'<w:pPr><w:keepNext/><w:spacing w:before="{360 if n < 3 else 200}" w:after="120"/><w:outlineLvl w:val="{n - 1}"/></w:pPr>'
    f'<w:rPr><w:b/><w:sz w:val="{ {1: 36, 2: 30, 3: 26, 4: 24, 5: 22, 6: 22}[n] }"/><w:szCs w:val="{ {1: 36, 2: 30, 3: 26, 4: 24, 5: 22, 6: 22}[n] }"/></w:rPr></w:style>'
    for n in range(1, 7))}
<w:style w:type="paragraph" w:styleId="ListParagraph"><w:name w:val="List Paragraph"/><w:basedOn w:val="Normal"/><w:qFormat/><w:pPr><w:spacing w:after="60"/><w:ind w:left="720"/></w:pPr></w:style>
<w:style w:type="table" w:default="1" w:styleId="TableNormal"><w:name w:val="Normal Table"/><w:uiPriority w:val="99"/><w:semiHidden/><w:tblPr><w:tblInd w:w="0" w:type="dxa"/><w:tblCellMar><w:top w:w="0" w:type="dxa"/><w:left w:w="108" w:type="dxa"/><w:bottom w:w="0" w:type="dxa"/><w:right w:w="108" w:type="dxa"/></w:tblCellMar></w:tblPr></w:style>
<w:style w:type="table" w:styleId="TableGrid"><w:name w:val="Table Grid"/><w:basedOn w:val="TableNormal"/><w:pPr><w:spacing w:after="0" w:line="240" w:lineRule="auto"/></w:pPr><w:tblPr><w:tblBorders>
<w:top w:val="single" w:sz="4" w:space="0" w:color="808080"/><w:left w:val="single" w:sz="4" w:space="0" w:color="808080"/><w:bottom w:val="single" w:sz="4" w:space="0" w:color="808080"/><w:right w:val="single" w:sz="4" w:space="0" w:color="808080"/>
<w:insideH w:val="single" w:sz="4" w:space="0" w:color="808080"/><w:insideV w:val="single" w:sz="4" w:space="0" w:color="808080"/></w:tblBorders></w:tblPr></w:style>
</w:styles>'''

_DOCX_NUMBERING_HEAD = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:numbering {_W_NS}>
<w:abstractNum w:abstractNumId="0"><w:multiLevelType w:val="hybridMultilevel"/><w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="bullet"/><w:lvlText w:val="&#8226;"/><w:lvlJc w:val="left"/><w:pPr><w:ind w:left="720" w:hanging="360"/></w:pPr><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri"/></w:rPr></w:lvl></w:abstractNum>
<w:abstractNum w:abstractNumId="1"><w:multiLevelType w:val="hybridMultilevel"/><w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/><w:lvlJc w:val="left"/><w:pPr><w:ind w:left="720" w:hanging="360"/></w:pPr></w:lvl></w:abstractNum>
<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>
'''


def _w_runs(text: str, bold_all: bool = False) -> str:
    out = []
    for t, b, i in _runs(text):
        if not t:
            continue
        props = ("<w:b/>" if (b or bold_all) else "") + ("<w:i/>" if i else "")
        out.append(f'<w:r>{"<w:rPr>" + props + "</w:rPr>" if props else ""}<w:t xml:space="preserve">{_x(t)}</w:t></w:r>')
    return "".join(out)


def _w_para(text: str, style: Optional[str] = None, num: Optional[int] = None, bold_all: bool = False) -> str:
    ppr = ""
    if style or num:
        ppr = "<w:pPr>" + (f'<w:pStyle w:val="{style}"/>' if style else "")
        ppr += f'<w:numPr><w:ilvl w:val="0"/><w:numId w:val="{num}"/></w:numPr>' if num else ""
        ppr += "</w:pPr>"
    return f"<w:p>{ppr}{_w_runs(text, bold_all)}</w:p>"


def _w_table(rows: list) -> str:
    cols = max(len(r) for r in rows)
    grid = "".join(f'<w:gridCol w="{9000 // cols}"/>' for _ in range(cols))
    out = ['<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/><w:tblW w:w="5000" w:type="pct"/></w:tblPr>'
           f"<w:tblGrid>{grid}</w:tblGrid>"]
    for n, row in enumerate(rows):
        out.append("<w:tr>" + ("<w:trPr><w:tblHeader/></w:trPr>" if n == 0 else ""))
        for c in range(cols):
            cell = row[c] if c < len(row) else ""
            shade = '<w:shd w:val="clear" w:color="auto" w:fill="E7E6E6"/>' if n == 0 else ""
            out.append(f'<w:tc><w:tcPr><w:tcW w:w="{9000 // cols}" w:type="dxa"/>{shade}</w:tcPr>'
                       f"{_w_para(cell, bold_all=(n == 0))}</w:tc>")
        out.append("</w:tr>")
    out.append("</w:tbl>")
    return "".join(out)


def _build_docx(text: str) -> tuple:
    blocks = parse_blocks(text)
    if not blocks:
        raise BuildError("there is no text to put in the Word document")
    body, nums, prev, n_para, n_table = [], [], None, 0, 0
    next_num = 3  # numId 1 = bullets, 2 reserved; every numbered list gets its own id so it restarts at 1
    current = None
    for b in blocks:
        kind = b[0]
        if kind == "heading":
            body.append(_w_para(b[2], style=f"Heading{b[1]}"))
        elif kind == "para":
            body.append(_w_para(b[1]))
        elif kind == "bullet":
            body.append(_w_para(b[1], style="ListParagraph", num=1))
        elif kind == "number":
            if prev != "number":
                current = next_num
                next_num += 1
                nums.append(current)
            body.append(_w_para(b[1], style="ListParagraph", num=current))
        elif kind == "table":
            body.append(_w_table(b[1]))
            body.append("<w:p/>")  # Word needs a paragraph between two tables and one at the end
            n_table += 1
        if kind != "table":
            n_para += 1
        prev = kind
    sect = ('<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
            '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" w:header="708" w:footer="708" w:gutter="0"/></w:sectPr>')
    document = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document {_W_NS}><w:body>'
                + "".join(body) + sect + "</w:body></w:document>")
    numbering = _DOCX_NUMBERING_HEAD + "".join(
        f'<w:num w:numId="{i}"><w:abstractNumId w:val="1"/><w:lvlOverride w:ilvl="0"><w:startOverride w:val="1"/></w:lvlOverride></w:num>'
        for i in nums) + "</w:numbering>"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml",
                    f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="{_CT_NS}">'
                    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                    '<Default Extension="xml" ContentType="application/xml"/>'
                    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                    '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
                    '<Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>'
                    "</Types>")
        zf.writestr("_rels/.rels",
                    f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="{_REL_NS}">'
                    f'<Relationship Id="rId1" Type="{_OD}/officeDocument" Target="word/document.xml"/></Relationships>')
        zf.writestr("word/_rels/document.xml.rels",
                    f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="{_REL_NS}">'
                    f'<Relationship Id="rId1" Type="{_OD}/styles" Target="styles.xml"/>'
                    f'<Relationship Id="rId2" Type="{_OD}/numbering" Target="numbering.xml"/></Relationships>')
        zf.writestr("word/document.xml", document)
        zf.writestr("word/styles.xml", _DOCX_STYLES)
        zf.writestr("word/numbering.xml", numbering)
    summary = f"a Word document ({n_para} paragraph{'s' if n_para != 1 else ''}"
    summary += f", {n_table} table{'s' if n_table != 1 else ''})" if n_table else ")"
    return buf.getvalue(), summary, ""


# ---------------------------------------------------------------- XLSX
_S_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_SHEET_MARK = re.compile(r"^\s*-{2,}\s*sheet:\s*(.+?)\s*-{2,}\s*$", re.I)
_NUMBER = re.compile(r"^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$")
_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_ISO_DATETIME = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?$")
_BAD_SHEET_CHARS = re.compile(r"[\\/*?:\[\]]")
_EPOCH = datetime(1899, 12, 30)


def _col_name(i: int) -> str:
    s, i = "", i + 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _serial(d: datetime) -> float:
    delta = d - _EPOCH
    return delta.days + delta.seconds / 86400


def _split_sheets(text: str) -> list:
    """[(name, [lines])]; text before any marker is the first sheet."""
    sheets: list = []
    name, lines = "Sheet1", []
    started = False
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        m = _SHEET_MARK.match(raw)
        if m:
            if started or lines:
                sheets.append((name, lines))
            name, lines, started = m.group(1), [], True
            continue
        lines.append(raw)
    sheets.append((name, lines))
    return [(n, [l for l in ls]) for n, ls in sheets if started or any(l.strip() for l in ls)] or [("Sheet1", [])]


def _clean_sheet_name(name: str, used: set) -> str:
    base = _BAD_SHEET_CHARS.sub(" ", name).strip().strip("'")[:31] or "Sheet"
    cand, n = base, 2
    while cand.lower() in used:
        suffix = f" ({n})"
        cand = base[:31 - len(suffix)] + suffix
        n += 1
    used.add(cand.lower())
    return cand


def _parse_row(line: str) -> list:
    if "\t" in line:
        return line.split("\t")
    return next(csv.reader([line], skipinitialspace=False))


def _cell_xml(ref: str, raw: str) -> str:
    v = raw.strip() if not raw.startswith('"') else raw
    if v == "":
        return ""
    if v.startswith("=") and len(v) > 1:
        return f'<c r="{ref}"><f>{_x(v[1:])}</f></c>'
    low = v.lower()
    if low in ("true", "false"):
        return f'<c r="{ref}" t="b"><v>{1 if low == "true" else 0}</v></c>'
    if _NUMBER.match(v) and not (len(v) > 1 and v.lstrip("+-").startswith("0") and v.lstrip("+-")[1:2].isdigit()):
        return f'<c r="{ref}"><v>{v}</v></c>'  # a leading-zero code like 007 stays text
    m = _ISO_DATE.match(v)
    if m:
        try:
            return f'<c r="{ref}" s="1"><v>{_serial(datetime(int(m[1]), int(m[2]), int(m[3]))):g}</v></c>'
        except ValueError:
            pass
    m = _ISO_DATETIME.match(v)
    if m:
        try:
            d = datetime(int(m[1]), int(m[2]), int(m[3]), int(m[4]), int(m[5]), int(m[6] or 0))
            return f'<c r="{ref}" s="2"><v>{_serial(d):.10g}</v></c>'
        except ValueError:
            pass
    return f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{_x(raw)}</t></is></c>'


def _build_xlsx(text: str) -> tuple:
    sheets = _split_sheets(text)
    used: set = set()
    parts, n_rows_total, n_formulas = [], 0, 0
    notes: list = []
    for name, lines in sheets:
        sheet_name = _clean_sheet_name(name, used)
        rows = []
        for r, line in enumerate(lines, 1):
            if r > MAX_XLSX_ROWS:
                notes.append(f"sheet '{sheet_name}' was cut at {MAX_XLSX_ROWS:,} rows")
                break
            if not line.strip():
                continue
            cells = _parse_row(line)[:MAX_XLSX_COLS]
            xml_cells = []
            for c, raw in enumerate(cells):
                cx = _cell_xml(f"{_col_name(c)}{r}", raw)
                if cx:
                    xml_cells.append(cx)
                    n_formulas += "<f>" in cx
            if xml_cells:
                rows.append(f'<row r="{r}">{"".join(xml_cells)}</row>')
                n_rows_total += 1
        parts.append((sheet_name, f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="{_S_NS}">'
                                  f'<sheetData>{"".join(rows)}</sheetData></worksheet>'))
    sheets_xml = "".join(f'<sheet name="{_attr(n)}" sheetId="{i}" r:id="rId{i}"/>' for i, (n, _) in enumerate(parts, 1))
    workbook = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="{_S_NS}" xmlns:r="{_OD}">'
                f'<sheets>{sheets_xml}</sheets><calcPr calcId="191029" fullCalcOnLoad="1"/></workbook>')
    wb_rels = "".join(f'<Relationship Id="rId{i}" Type="{_OD}/worksheet" Target="worksheets/sheet{i}.xml"/>'
                      for i in range(1, len(parts) + 1))
    wb_rels += f'<Relationship Id="rId{len(parts) + 1}" Type="{_OD}/styles" Target="styles.xml"/>'
    styles = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><styleSheet xmlns="{_S_NS}">'
              '<numFmts count="1"><numFmt numFmtId="164" formatCode="yyyy\\-mm\\-dd\\ hh:mm"/></numFmts>'
              '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
              '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
              '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
              '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
              '<cellXfs count="3"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
              '<xf numFmtId="14" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
              '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/></cellXfs>'
              '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')
    ct = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="{_CT_NS}">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="xml" ContentType="application/xml"/>'
          '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
          '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
          + "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                    for i in range(1, len(parts) + 1)) + "</Types>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", ct)
        zf.writestr("_rels/.rels", f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="{_REL_NS}">'
                                   f'<Relationship Id="rId1" Type="{_OD}/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        zf.writestr("xl/workbook.xml", workbook)
        zf.writestr("xl/_rels/workbook.xml.rels",
                    f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="{_REL_NS}">{wb_rels}</Relationships>')
        zf.writestr("xl/styles.xml", styles)
        for i, (_, xml) in enumerate(parts, 1):
            zf.writestr(f"xl/worksheets/sheet{i}.xml", xml)
    summary = (f"an Excel workbook ({len(parts)} sheet{'s' if len(parts) != 1 else ''}, {n_rows_total} row"
               f"{'s' if n_rows_total != 1 else ''}"
               + (f", {n_formulas} formula{'s' if n_formulas != 1 else ''} that Excel calculates when it opens the file" if n_formulas else "")
               + ")")
    return buf.getvalue(), summary, ("; ".join(notes) + "." if notes else "")


# ---------------------------------------------------------------- PDF
_FONT_CANDIDATES = [  # (regular, bold, italic, bold-italic) -- the first family whose regular file exists wins
    ("segoeui.ttf", "segoeuib.ttf", "segoeuii.ttf", "segoeuiz.ttf"),
    ("arial.ttf", "arialbd.ttf", "ariali.ttf", "arialbi.ttf"),
    ("calibri.ttf", "calibrib.ttf", "calibrii.ttf", "calibriz.ttf"),
    ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf", "DejaVuSans-Oblique.ttf", "DejaVuSans-BoldOblique.ttf"),
    ("LiberationSans-Regular.ttf", "LiberationSans-Bold.ttf", "LiberationSans-Italic.ttf", "LiberationSans-BoldItalic.ttf"),
]


def _font_dirs() -> list:
    dirs = []
    override = os.environ.get("JARVIS_PDF_FONT_DIR")
    if override:
        dirs.append(Path(override))
    windir = os.environ.get("WINDIR") or os.environ.get("SystemRoot")
    if windir:
        dirs.append(Path(windir) / "Fonts")
    dirs += [Path("/usr/share/fonts/truetype/dejavu"), Path("/usr/share/fonts/truetype/liberation"),
             Path("/usr/share/fonts/TTF"), Path("/Library/Fonts"), Path("/System/Library/Fonts/Supplemental")]
    return [d for d in dirs if d.is_dir()]


def _find_font_family() -> Optional[tuple]:
    dirs = _font_dirs()
    for family in _FONT_CANDIDATES:
        for d in dirs:
            files = [d / f for f in family]
            if files[0].is_file():
                return tuple(f if f.is_file() else files[0] for f in files)
    return None


def _register_fonts():
    """(regular, bold, italic, bold-italic font names, can_draw(ch)) -- a system TTF family, else Helvetica."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    family = _find_font_family()
    if family:
        try:
            names = []
            for tag, path in zip(("R", "B", "I", "BI"), family):
                name = f"JarvisPdf-{family[0].stem}-{tag}"
                if name not in pdfmetrics.getRegisteredFontNames():
                    pdfmetrics.registerFont(TTFont(name, str(path)))
                names.append(name)
            face = pdfmetrics.getFont(names[0]).face
            cmap = face.charToGlyph
            return (*names, lambda ch: ord(ch) in cmap or ch in "\n\t")
        except Exception:
            pass  # a damaged font file: fall through to Helvetica
    def helv(ch: str) -> bool:
        try:
            ch.encode("cp1252")
            return True
        except UnicodeEncodeError:
            return False
    return ("Helvetica", "Helvetica-Bold", "Helvetica-Oblique", "Helvetica-BoldOblique", helv)


def _pdf_markup(text: str) -> str:
    out = []
    for t, b, i in _runs(text):
        s = _esc(t)
        if b:
            s = f"<b>{s}</b>"
        if i:
            s = f"<i>{s}</i>"
        out.append(s)
    return "".join(out)


def _build_pdf(text: str) -> tuple:
    blocks = parse_blocks(text)
    if not blocks:
        raise BuildError("there is no text to put in the PDF")
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    except ImportError:
        raise BuildError("PDF writing needs the 'reportlab' package, which is not installed in Jarvis's Python")
    regular, bold, italic, bold_italic, can_draw = _register_fonts()
    pdfmetrics.registerFontFamily(regular, normal=regular, bold=bold, italic=italic, boldItalic=bold_italic)

    lost: dict = {}

    def clean(s: str) -> str:
        s = _XML_BAD.sub("", s)
        out = []
        for ch in s:
            if can_draw(ch):
                out.append(ch)
            else:
                lost[ch] = lost.get(ch, 0) + 1
                out.append("?")
        return "".join(out)

    body = ParagraphStyle("body", fontName=regular, fontSize=10.5, leading=15, spaceAfter=6)
    heads = {n: ParagraphStyle(f"h{n}", parent=body, fontName=bold, fontSize=s, leading=s + 5, spaceBefore=12 if n > 2 else 16,
                               spaceAfter=6, keepWithNext=1)
             for n, s in {1: 22, 2: 17, 3: 14, 4: 12.5, 5: 11.5, 6: 11}.items()}
    item = ParagraphStyle("item", parent=body, leftIndent=18, bulletIndent=6, spaceAfter=3, bulletFontName=regular)
    cell = ParagraphStyle("cell", parent=body, fontSize=9.5, leading=12.5, spaceAfter=0)
    cell_head = ParagraphStyle("cellh", parent=cell, fontName=bold)

    story, number = [], 0
    n_paras = n_tables = 0
    for b in blocks:
        kind = b[0]
        if kind != "number":
            number = 0
        if kind == "heading":
            story.append(Paragraph(_pdf_markup(clean(b[2])), heads[b[1]]))
        elif kind == "para":
            story.append(Paragraph(_pdf_markup(clean(b[1])), body))
        elif kind == "bullet":
            story.append(Paragraph(_pdf_markup(clean(b[1])), item, bulletText="•" if can_draw("•") else "-"))
        elif kind == "number":
            number += 1
            story.append(Paragraph(_pdf_markup(clean(b[1])), item, bulletText=f"{number}."))
        elif kind == "table":
            rows = b[1]
            cols = max(len(r) for r in rows)
            data = [[Paragraph(_pdf_markup(clean(r[c] if c < len(r) else "")), cell_head if n == 0 else cell) for c in range(cols)]
                    for n, r in enumerate(rows)]
            weights = [max(6, min(40, max(len((r[c] if c < len(r) else "")) for r in rows))) for c in range(cols)]
            usable = A4[0] - 40 * mm
            widths = [usable * w / sum(weights) for w in weights]
            t = Table(data, colWidths=widths, repeatRows=1)
            t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.Color(.5, .5, .5)),
                                   ("BACKGROUND", (0, 0), (-1, 0), colors.Color(.9, .9, .9)),
                                   ("VALIGN", (0, 0), (-1, -1), "TOP"),
                                   ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
            story.extend([t, Spacer(1, 8)])
            n_tables += 1
        if kind != "table":
            n_paras += 1
    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=20 * mm, bottomMargin=20 * mm,
                      title="", author="").build(story)
    data = buf.getvalue()
    pages = len(re.findall(rb"/Type\s*/Page[^s]", data))
    summary = f"a PDF ({pages} page{'s' if pages != 1 else ''}, {n_paras} paragraph{'s' if n_paras != 1 else ''}"
    summary += f", {n_tables} table{'s' if n_tables != 1 else ''})" if n_tables else ")"
    note = ""
    if lost:
        shown = " ".join(f"{ch!r}" for ch in list(lost)[:8])
        note = (f"Warning: {sum(lost.values())} character(s) the PDF font can't draw were replaced by '?': {shown}"
                f"{' ...' if len(lost) > 8 else ''}. Use a Word file if the text needs them.")
    return data, summary, note


# ---------------------------------------------------------------- entry point
def build_document(extension: str, content: str) -> tuple:
    """(file bytes, 'a Word document (...)', note) for '.docx' / '.xlsx' / '.pdf'. Raises BuildError."""
    text = str(content or "")
    if not text.strip():
        raise BuildError("the content is empty")
    if len(text) > MAX_CONTENT_CHARS:
        raise BuildError(f"the content is {len(text):,} characters, over the {MAX_CONTENT_CHARS:,} limit")
    ext = extension.lower()
    if ext == ".docx":
        return _build_docx(text)
    if ext == ".xlsx":
        return _build_xlsx(text)
    if ext == ".pdf":
        return _build_pdf(text)
    raise BuildError(f"{extension} files are not supported")


def write_bytes_atomic(path: Path, data: bytes) -> None:
    """Temp file beside the target, flushed, then swapped in -- a crash leaves the old file (see app.atomic_write)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
