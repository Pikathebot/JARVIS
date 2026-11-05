"""
Build Word, Excel, PowerPoint and PDF files from text the model writes (PLAN 4.27, writing).

``write_file`` calls ``build_document`` for a ``.docx`` / ``.xlsx`` / ``.pptx`` / ``.pdf`` path, so no new tool or approval tier
exists: replacing a file still asks and backs up first. CSV needs nothing here (it is text).

The text conventions are the ones ``doc_readers`` prints, and ``prepare_text`` takes ``read_file``'s own output back (its
header line, page and sheet markers, row numbers, ``=F -> cached value``), so read -> change -> write works. What does not survive a
round trip: numbered lists come back as bullets, page breaks, hidden-sheet state, fonts/colours/column widths, cached formula
values (Excel recalculates), anything the reader doesn't show (images, headers/footers, comments). Text that ``read_file`` cut
(``more rows not read``, a line range, ``characters omitted``) is refused, because writing it back would drop the unread part.

* Word and PDF: Markdown, one paragraph per line. ``#``..``######`` headings, ``-``/``*`` bullets, ``1.`` numbered items,
  ``| a | b |`` table rows (first row is the header), ``**bold**`` and ``*italic*``.
* PowerPoint: the same Markdown; a ``---`` line or a new ``# `` heading starts a slide, ``# `` is its title, a ``| `` table
  becomes a table, and a ``Notes:`` line (and what follows it on that slide) becomes the speaker notes.
* Excel: CSV rows (a tab-separated line works too); ``--- sheet: Name ---`` starts a sheet. ``=SUM(A1:A3)`` is a formula
  (Excel recalculates on open), numbers are numbers, ``2026-10-03`` is a date, ``TRUE``/``FALSE`` are booleans.

Word, Excel and PowerPoint are written straight to OOXML with ``zipfile`` (no dependency). PDF uses ``reportlab``, imported on first use,
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

BUILDABLE_EXTENSIONS = frozenset({".docx", ".xlsx", ".pdf", ".pptx"})
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
_BULLET = re.compile(r"^\s*[-*+\u2022]\s+(.*)$")  # •: what a PDF read back shows for a bullet
_NUMBERED = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_RULE = re.compile(r"^\s*\|?(\s*:?-{2,}:?\s*\|)+\s*:?-*:?\s*$")
_INLINE = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*|\*(?=\S)(.+?)(?<=\S)\*")


def parse_blocks(text: str) -> list:
    """[('heading', level, text) | ('para', text) | ('bullet', text) | ('number', text) | ('table', [[cell]])].
    Every non-blank line is its own paragraph (what read_file prints, and what a model writing a document means)."""
    blocks: list = []
    table: list = []

    def flush_table():
        if table:
            blocks.append(("table", [list(r) for r in table]))
            table.clear()

    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.rstrip()
        if _TABLE_ROW.match(line):
            if not _TABLE_RULE.match(line):
                table.append([c.strip() for c in line.strip().strip("|").split("|")])
            continue
        flush_table()
        if not line.strip():
            continue
        m = _HEADING.match(line)
        if m:
            blocks.append(("heading", len(m.group(1)), m.group(2)))
            continue
        m = _BULLET.match(line)
        if m:
            blocks.append(("bullet", m.group(1)))
            continue
        m = _NUMBERED.match(line)
        if m:
            blocks.append(("number", m.group(1)))
            continue
        blocks.append(("para", line.strip()))
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


# ---------------------------------------------------------------- taking read_file's output back
from app.agent.tools.doc_readers import CUT_RE as _CUT, HEADER_RE as _HEADER, SCAFFOLD_RE as _READBACK_LINE  # noqa: E402

_READFILE_NOTE = re.compile(r"^\[Nothing at '.*'; this is '.*'\]\s*$")
_LINES_NOTE = re.compile(r"^\[Lines (\d+)-(\d+) of (\d+) in '.*'\]\s*$")


def prepare_text(text: str, keep_slides: bool = False) -> tuple:
    """(text without read_file's own scaffolding, readback). ``readback`` is True when the text is a document read with
    read_file (it starts with the reader's header), which switches on row numbers and ``=F -> value`` handling for Excel.
    Raises BuildError for text that read_file cut short."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if "cells past column" in text and _CUT.search(text):
        raise BuildError("the sheet is wider than read_file shows (it stops at 100 columns), so writing this text back would "
                         "delete the columns it never saw. Jarvis can't rewrite this workbook: write the result to a new file, "
                         "or tell the user")
    if _CUT.search(text):
        raise BuildError("the text has a cut marker ('more rows not read', 'characters omitted ...'): it is only part of the "
                         "document, and writing it back would drop the part that was never read. Read the whole file "
                         "(in line ranges if it is long) and send everything")
    out, readback = [], False
    for line in lines:
        m = _LINES_NOTE.match(line)
        if m:
            first, last, total = (int(g) for g in m.groups())
            if first != 1 or last < total:
                raise BuildError(f"the text is lines {first}-{last} of {total} from read_file, only part of the document: "
                                 "writing it back would drop the rest. Send the whole document")
            continue
        if _READFILE_NOTE.match(line):
            continue
        if _HEADER.match(line):
            readback = True
            continue
        out.append(line)
    def scaffold(line: str) -> bool:
        return bool(_READBACK_LINE.match(line)) and not (keep_slides and _SLIDE_MARK.match(line))  # a deck needs its slide markers

    if readback:
        out = [l for l in out if not scaffold(l)]
    elif any(_READBACK_LINE.match(l) and l.startswith("(row number first") for l in out):
        readback = True
        out = [l for l in out if not scaffold(l)]
    return "\n".join(out), readback


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


_HIDDEN = re.compile(r"\s*\(hidden\)\s*$")
_ROW_PREFIX = re.compile(r"^(\d+):(?: (.*))?$")


def _split_sheets(text: str, readback: bool = False) -> list:
    """[(name, [lines])]; text before any marker is the first sheet."""
    sheets: list = []
    name, lines = "Sheet1", []
    started = False
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        m = _SHEET_MARK.match(raw)
        if m:
            if started or lines:
                sheets.append((name, lines))
            name, lines, started = (_HIDDEN.sub("", m.group(1)) if readback else m.group(1)), [], True
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
    if (_NUMBER.match(v) and not (len(v) > 1 and v.lstrip("+-").startswith("0") and v.lstrip("+-")[1:2].isdigit())
            and len(re.sub(r"[eE].*$", "", v).replace(".", "").lstrip("+-0")) <= 15):
        # a leading-zero code like 007 stays text, and so does a long ID: Excel keeps 15 significant digits and would round it
        return f'<c r="{ref}"><v>{v}</v></c>'
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


def _build_xlsx(text: str, readback: bool = False) -> tuple:
    sheets = _split_sheets(text, readback)
    used: set = set()
    parts, n_rows_total, n_formulas = [], 0, 0
    notes: list = []
    for name, lines in sheets:
        sheet_name = _clean_sheet_name(name, used)
        rows = []
        r = 0
        for line in lines:
            r += 1
            if readback:
                m = _ROW_PREFIX.match(line)
                if m:  # read_file's "7: a<TAB>b": the number is the row, so gaps and formula references stay where they were
                    r, line = int(m.group(1)), m.group(2) or ""
                cells = [c for c in line.split("\t")] if line else []
                cells = [c.rsplit(" -> ", 1)[0] if c.startswith("=") and " -> " in c else c for c in cells]
            else:
                cells = _parse_row(line) if line.strip() else []
            if r > MAX_XLSX_ROWS:
                notes.append(f"sheet '{sheet_name}' was cut at {MAX_XLSX_ROWS:,} rows")
                break
            if not any(c.strip() for c in cells):
                continue
            if len(cells) > MAX_XLSX_COLS and any(c.strip() for c in cells[MAX_XLSX_COLS:]):
                note = f"sheet '{sheet_name}': columns past {MAX_XLSX_COLS} were left out"
                if note not in notes:
                    notes.append(note)
            xml_cells = []
            for c, raw in enumerate(cells[:MAX_XLSX_COLS]):
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


# ---------------------------------------------------------------- PPTX
_SLIDE_MARK = re.compile(r"^\s*-{2,}\s*slide\s*\d*\s*-{2,}\s*$", re.I)
_HR = re.compile(r"^\s*-{3,}\s*$")
_H1 = re.compile(r"^#\s+\S")
_A_NS = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
_P_NS = 'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
_R_NS = f'xmlns:r="{_OD}"'
_SLIDE_W, _SLIDE_H = 12192000, 6858000
_MARGIN_X = 609600
_TITLE_Y, _TITLE_H = 304800, 1066800
_BODY_Y, _BODY_H = 1524000, 4572000
_BODY_W = _SLIDE_W - 2 * _MARGIN_X
_BOTTOM = 6553200                          # nothing should run past this
_FONT_SIZES = (2800, 2400, 2200, 2000, 1800, 1600, 1400, 1200)
_GRP = ('<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr><a:xfrm><a:off x="0" y="0"/>'
        '<a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>')
_NOTES_RE = re.compile(r"^\s*notes\s*:\s*(.*)$", re.I)

_THEME = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<a:theme {_A_NS} name="Jarvis"><a:themeElements><a:clrScheme name="Jarvis">
<a:dk1><a:sysClr val="windowText" lastClr="000000"/></a:dk1><a:lt1><a:sysClr val="window" lastClr="FFFFFF"/></a:lt1>
<a:dk2><a:srgbClr val="1F2A44"/></a:dk2><a:lt2><a:srgbClr val="EEECE1"/></a:lt2>
<a:accent1><a:srgbClr val="2F5597"/></a:accent1><a:accent2><a:srgbClr val="C55A11"/></a:accent2><a:accent3><a:srgbClr val="548235"/></a:accent3>
<a:accent4><a:srgbClr val="7F6000"/></a:accent4><a:accent5><a:srgbClr val="7030A0"/></a:accent5><a:accent6><a:srgbClr val="C00000"/></a:accent6>
<a:hlink><a:srgbClr val="0563C1"/></a:hlink><a:folHlink><a:srgbClr val="954F72"/></a:folHlink></a:clrScheme>
<a:fontScheme name="Jarvis"><a:majorFont><a:latin typeface="Calibri"/><a:ea typeface=""/><a:cs typeface=""/></a:majorFont>
<a:minorFont><a:latin typeface="Calibri"/><a:ea typeface=""/><a:cs typeface=""/></a:minorFont></a:fontScheme>
<a:fmtScheme name="Jarvis"><a:fillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:fillStyleLst>
<a:lnStyleLst><a:ln w="9525"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln><a:ln w="19050"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln><a:ln w="28575"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln></a:lnStyleLst>
<a:effectStyleLst><a:effectStyle><a:effectLst/></a:effectStyle><a:effectStyle><a:effectLst/></a:effectStyle><a:effectStyle><a:effectLst/></a:effectStyle></a:effectStyleLst>
<a:bgFillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:bgFillStyleLst>
</a:fmtScheme></a:themeElements></a:theme>'''

_CLRMAP = ('<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" '
           'accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/>')


def _ph_sp(sid: int, name: str, ph: str, x: int, y: int, w: int, h: int, body: str = "") -> str:
    return (f'<p:sp><p:nvSpPr><p:cNvPr id="{sid}" name="{name}"/><p:cNvSpPr><a:spLocks noGrp="1"/></p:cNvSpPr><p:nvPr>{ph}</p:nvPr></p:nvSpPr>'
            f'<p:spPr><a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{w}" cy="{h}"/></a:xfrm></p:spPr>{body}</p:sp>')


_MASTER = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><p:sldMaster {_A_NS} {_R_NS} {_P_NS}><p:cSld>'
           '<p:bg><p:bgRef idx="1001"><a:schemeClr val="bg1"/></p:bgRef></p:bg><p:spTree>' + _GRP
           + _ph_sp(2, "Title Placeholder 1", '<p:ph type="title"/>', _MARGIN_X, _TITLE_Y, _BODY_W, _TITLE_H,
                    '<p:txBody><a:bodyPr anchor="ctr"><a:normAutofit/></a:bodyPr><a:lstStyle/><a:p><a:r><a:rPr lang="en-US"/><a:t>Title</a:t></a:r></a:p></p:txBody>')
           + _ph_sp(3, "Text Placeholder 2", '<p:ph type="body" idx="1"/>', _MARGIN_X, _BODY_Y, _BODY_W, _BODY_H,
                    '<p:txBody><a:bodyPr><a:normAutofit/></a:bodyPr><a:lstStyle/><a:p><a:pPr lvl="0"/><a:r><a:rPr lang="en-US"/><a:t>Text</a:t></a:r></a:p></p:txBody>')
           + f'</p:spTree></p:cSld>{_CLRMAP}<p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst><p:txStyles>'
           '<p:titleStyle><a:lvl1pPr algn="l"><a:defRPr sz="3600" b="1" kern="1200"><a:solidFill><a:schemeClr val="tx2"/></a:solidFill>'
           '<a:latin typeface="+mj-lt"/><a:ea typeface="+mj-ea"/><a:cs typeface="+mj-cs"/></a:defRPr></a:lvl1pPr></p:titleStyle>'
           '<p:bodyStyle>' + "".join(
               f'<a:lvl{n}pPr marL="{342900 * n}" indent="-342900" algn="l"><a:spcBef><a:spcPts val="600"/></a:spcBef><a:buFont typeface="Arial"/>'
               f'<a:buChar char="&#8226;"/><a:defRPr sz="{2400 - 200 * (n - 1)}" kern="1200"><a:solidFill><a:schemeClr val="tx1"/></a:solidFill>'
               '<a:latin typeface="+mn-lt"/><a:ea typeface="+mn-ea"/><a:cs typeface="+mn-cs"/></a:defRPr>' + f'</a:lvl{n}pPr>' for n in range(1, 4))
           + '</p:bodyStyle><p:otherStyle><a:defPPr><a:defRPr lang="en-US"/></a:defPPr></p:otherStyle></p:txStyles></p:sldMaster>')

_LAYOUT = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><p:sldLayout {_A_NS} {_R_NS} {_P_NS} type="obj" preserve="1">'
           '<p:cSld name="Title and Content"><p:spTree>' + _GRP
           + _ph_sp(2, "Title 1", '<p:ph type="title"/>', _MARGIN_X, _TITLE_Y, _BODY_W, _TITLE_H,
                    '<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:rPr lang="en-US"/><a:t>Click to edit the title</a:t></a:r></a:p></p:txBody>')
           + _ph_sp(3, "Content Placeholder 2", '<p:ph idx="1"/>', _MARGIN_X, _BODY_Y, _BODY_W, _BODY_H,
                    '<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:pPr lvl="0"/><a:r><a:rPr lang="en-US"/><a:t>Click to edit the text</a:t></a:r></a:p></p:txBody>')
           + '</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>')

_NOTES_MASTER = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><p:notesMaster {_A_NS} {_R_NS} {_P_NS}><p:cSld>'
                 '<p:bg><p:bgRef idx="1001"><a:schemeClr val="bg1"/></p:bgRef></p:bg><p:spTree>' + _GRP
                 + _ph_sp(2, "Slide Image Placeholder 1", '<p:ph type="sldImg" idx="2"/>', 685800, 1143000, 5486400, 3086100)
                 + _ph_sp(3, "Notes Placeholder 2", '<p:ph type="body" sz="quarter" idx="3"/>', 685800, 4400550, 5486400, 3600450,
                          '<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:pPr lvl="0"/><a:r><a:rPr lang="en-US"/><a:t>Notes</a:t></a:r></a:p></p:txBody>')
                 + f'</p:spTree></p:cSld>{_CLRMAP}<p:notesStyle><a:lvl1pPr marL="0" algn="l"><a:defRPr sz="1200" kern="1200">'
                 '<a:solidFill><a:schemeClr val="tx1"/></a:solidFill><a:latin typeface="+mn-lt"/></a:defRPr></a:lvl1pPr></p:notesStyle></p:notesMaster>')


def _a_runs(text: str, size: Optional[int] = None, bold_all: bool = False) -> str:
    out = []
    for t, b, i in _runs(text):
        if not t:
            continue
        attrs = 'lang="en-US"' + (f' sz="{size}"' if size else "") + (' b="1"' if (b or bold_all) else "") + (' i="1"' if i else "")
        out.append(f'<a:r><a:rPr {attrs} dirty="0"/><a:t>{_x(t)}</a:t></a:r>')
    return "".join(out) or '<a:endParaRPr lang="en-US"/>'


def _text_lines(blocks: list, size: int) -> int:
    """How many lines the blocks take at ``size`` (hundredths of a point) in the body placeholder: about half an em per character."""
    per_line = max(10, int((_BODY_W - 457200) / (size / 100 * 12700 * 0.5)))
    return sum(max(1, -(-len(b[-1]) // per_line)) if isinstance(b[-1], str) else 0 for b in blocks)


def _fit_size(blocks: list, height: int) -> tuple:
    """(font size, fits) -- the biggest size whose estimated text height fits ``height`` EMU."""
    for size in _FONT_SIZES:
        if _text_lines(blocks, size) * (size / 100 * 12700 * 1.25 + 76200) <= height:
            return size, True
    return _FONT_SIZES[-1], False


def _slide_table(sid: int, rows: list, y: int) -> tuple:
    """(graphicFrame xml, its height). Column widths follow the longest text in each column; row heights follow the wrapped lines."""
    cols = max(len(r) for r in rows)
    weights = [max(6, min(40, max(len(r[c]) if c < len(r) else 0 for r in rows))) for c in range(cols)]
    widths = [_BODY_W * w // sum(weights) for w in weights]
    size = 1600 if len(rows) <= 8 else 1400
    row_xml, total = [], 0
    for n, row in enumerate(rows):
        lines = max(max(1, -(-int(len(row[c] if c < len(row) else "") * size / 100 * 12700 * 0.5) // max(1, widths[c] - 182880)))
                    for c in range(cols))
        h = int(lines * size / 100 * 12700 * 1.2 + 182880)
        cells = []
        for c in range(cols):
            border = "".join(f'<a:ln{k} w="12700"><a:solidFill><a:srgbClr val="808080"/></a:solidFill></a:ln{k}>' for k in "LRTB")
            fill = '<a:solidFill><a:srgbClr val="E7E6E6"/></a:solidFill>' if n == 0 else ""
            cells.append(f'<a:tc><a:txBody><a:bodyPr/><a:lstStyle/><a:p>{_a_runs(row[c] if c < len(row) else "", size, n == 0)}</a:p></a:txBody>'
                         f"<a:tcPr>{border}{fill}</a:tcPr></a:tc>")
        row_xml.append(f'<a:tr h="{h}">{"".join(cells)}</a:tr>')
        total += h
    grid = "".join(f'<a:gridCol w="{w}"/>' for w in widths)
    frame = (f'<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="{sid}" name="Table {sid}"/><p:cNvGraphicFramePr><a:graphicFrameLocks noGrp="1"/>'
             f'</p:cNvGraphicFramePr><p:nvPr/></p:nvGraphicFramePr><p:xfrm><a:off x="{_MARGIN_X}" y="{y}"/><a:ext cx="{sum(widths)}" cy="{total}"/></p:xfrm>'
             f'<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/table"><a:tbl><a:tblPr firstRow="1"/>'
             f'<a:tblGrid>{grid}</a:tblGrid>{"".join(row_xml)}</a:tbl></a:graphicData></a:graphic></p:graphicFrame>')
    return frame, total


def _split_slides(text: str, readback: bool) -> list:
    """[[line]] per slide: a '--- slide N ---' marker or (written fresh) a lone '---' or a '# ' heading starts the next slide."""
    slides: list = [[]]
    for line in text.split("\n"):
        if _SLIDE_MARK.match(line) or (not readback and (_HR.match(line) or (_H1.match(line) and any(l.strip() for l in slides[-1])))):
            if _SLIDE_MARK.match(line) or _HR.match(line):
                if any(l.strip() for l in slides[-1]) or len(slides) > 1:
                    slides.append([])
                continue
            slides.append([line])
            continue
        slides[-1].append(line)
    return [s for s in slides if any(l.strip() for l in s)]


def _build_pptx(text: str, readback: bool = False) -> tuple:
    raw_slides = _split_slides(text, readback)
    if not raw_slides:
        raise BuildError("there is no text to put on a slide")
    slides, warnings = [], []
    for n, lines in enumerate(raw_slides, 1):
        blocks = parse_blocks("\n".join(lines))
        title, notes = "", []
        if blocks and blocks[0][0] == "heading" and (blocks[0][1] <= 2 or not readback):
            title, blocks = blocks[0][2], blocks[1:]
        elif readback and blocks and blocks[0][0] in ("para", "number"):
            title, blocks = blocks[0][1], blocks[1:]       # read_file prints the title first, as a plain line ('- ' is body text)
        for i, b in enumerate(blocks):
            if b[0] in ("para", "bullet") and _NOTES_RE.match(b[1]):
                notes = [_NOTES_RE.match(b[1]).group(1)] + [x[-1] for x in blocks[i + 1:] if isinstance(x[-1], str)]
                blocks = blocks[:i]
                break
        slides.append({"title": title, "blocks": blocks, "notes": [t for t in notes if t.strip()]})
    has_notes = any(s["notes"] for s in slides)
    n_tables = n_notes = 0
    parts: dict = {}
    for n, s in enumerate(slides, 1):
        text_blocks = [b for b in s["blocks"] if b[0] != "table"]
        tables = [b[1] for b in s["blocks"] if b[0] == "table"]
        y_top = _BODY_Y
        shapes = []
        sid = 2
        if s["title"]:
            shapes.append(_ph_sp(sid, "Title 1", '<p:ph type="title"/>', _MARGIN_X, _TITLE_Y, _BODY_W, _TITLE_H,
                                 f'<p:txBody><a:bodyPr><a:normAutofit/></a:bodyPr><a:lstStyle/><a:p>{_a_runs(s["title"])}</a:p></p:txBody>'))
            sid += 1
        if text_blocks:
            size, fits = _fit_size(text_blocks, _BODY_H if not tables else int(_BODY_H * 0.5))
            if not fits:
                warnings.append(f"slide {n} has more text than fits; split it")
            height = _BODY_H if not tables else int(_BODY_H * 0.5)
            paras, counter = [], 0
            for b in text_blocks:
                kind = b[0]
                counter = counter + 1 if kind == "number" else 0
                if kind == "heading":
                    paras.append(f'<a:p><a:pPr marL="0" indent="0"><a:buNone/></a:pPr>{_a_runs(b[2], size, True)}</a:p>')
                elif kind == "para":
                    paras.append(f'<a:p><a:pPr marL="0" indent="0"><a:buNone/></a:pPr>{_a_runs(b[1], size)}</a:p>')
                elif kind == "number":
                    paras.append(f'<a:p><a:pPr marL="457200" indent="-457200"><a:buFont typeface="+mj-lt"/><a:buAutoNum type="arabicPeriod"/></a:pPr>'
                                 f"{_a_runs(b[1], size)}</a:p>")
                else:
                    paras.append(f"<a:p>{_a_runs(b[1], size)}</a:p>")
            shapes.append(_ph_sp(sid, "Content Placeholder 2", '<p:ph idx="1"/>', _MARGIN_X, _BODY_Y, _BODY_W, height,
                                 f'<p:txBody><a:bodyPr><a:normAutofit/></a:bodyPr><a:lstStyle/>{"".join(paras)}</p:txBody>'))
            sid += 1
            if tables:
                y_top = _BODY_Y + min(height, int(_text_lines(text_blocks, size) * (size / 100 * 12700 * 1.25 + 76200))) + 152400
        for rows in tables:
            frame, h = _slide_table(sid, rows, y_top)
            shapes.append(frame)
            sid += 1
            y_top += h + 152400
            n_tables += 1
        if tables and y_top - 152400 > _BOTTOM:
            warnings.append(f"slide {n} has a table that runs past the bottom; split it")
        rels = [f'<Relationship Id="rId1" Type="{_OD}/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>']
        if s["notes"]:
            n_notes += 1
            rels.append(f'<Relationship Id="rId2" Type="{_OD}/notesSlide" Target="../notesSlides/notesSlide{n}.xml"/>')
            note_paras = "".join(f'<a:p>{_a_runs(t)}</a:p>' for t in s["notes"])
            parts[f"ppt/notesSlides/notesSlide{n}.xml"] = (
                f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><p:notes {_A_NS} {_R_NS} {_P_NS}><p:cSld><p:spTree>{_GRP}'
                '<p:sp><p:nvSpPr><p:cNvPr id="2" name="Slide Image Placeholder 1"/><p:cNvSpPr><a:spLocks noGrp="1" noRot="1" noChangeAspect="1"/></p:cNvSpPr>'
                '<p:nvPr><p:ph type="sldImg"/></p:nvPr></p:nvSpPr><p:spPr/></p:sp>'
                '<p:sp><p:nvSpPr><p:cNvPr id="3" name="Notes Placeholder 2"/><p:cNvSpPr><a:spLocks noGrp="1"/></p:cNvSpPr>'
                '<p:nvPr><p:ph type="body" idx="1"/></p:nvPr></p:nvSpPr><p:spPr/>'
                f'<p:txBody><a:bodyPr/><a:lstStyle/>{note_paras}</p:txBody></p:sp></p:spTree></p:cSld>'
                '<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:notes>')
            parts[f"ppt/notesSlides/_rels/notesSlide{n}.xml.rels"] = (
                f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="{_REL_NS}">'
                f'<Relationship Id="rId1" Type="{_OD}/notesMaster" Target="../notesMasters/notesMaster1.xml"/>'
                f'<Relationship Id="rId2" Type="{_OD}/slide" Target="../slides/slide{n}.xml"/></Relationships>')
        parts[f"ppt/slides/slide{n}.xml"] = (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><p:sld {_A_NS} {_R_NS} {_P_NS}><p:cSld><p:spTree>{_GRP}'
            f'{"".join(shapes)}</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>')
        parts[f"ppt/slides/_rels/slide{n}.xml.rels"] = (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="{_REL_NS}">{"".join(rels)}</Relationships>')
    count = len(slides)
    pres_rels = [f'<Relationship Id="rId1" Type="{_OD}/slideMaster" Target="slideMasters/slideMaster1.xml"/>']
    pres_rels += [f'<Relationship Id="rId{i + 1}" Type="{_OD}/slide" Target="slides/slide{i}.xml"/>' for i in range(1, count + 1)]
    pres_rels.append(f'<Relationship Id="rId{count + 2}" Type="{_OD}/theme" Target="theme/theme1.xml"/>')
    notes_list = ""
    if has_notes:
        pres_rels.append(f'<Relationship Id="rId{count + 3}" Type="{_OD}/notesMaster" Target="notesMasters/notesMaster1.xml"/>')
        notes_list = f'<p:notesMasterIdLst><p:notesMasterId r:id="rId{count + 3}"/></p:notesMasterIdLst>'
        parts["ppt/notesMasters/notesMaster1.xml"] = _NOTES_MASTER
        parts["ppt/notesMasters/_rels/notesMaster1.xml.rels"] = (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="{_REL_NS}">'
            f'<Relationship Id="rId1" Type="{_OD}/theme" Target="../theme/theme2.xml"/></Relationships>')
        parts["ppt/theme/theme2.xml"] = _THEME
    ids = "".join(f'<p:sldId id="{255 + i}" r:id="rId{i + 1}"/>' for i in range(1, count + 1))
    parts["ppt/presentation.xml"] = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><p:presentation {_A_NS} {_R_NS} {_P_NS} saveSubsetFonts="1">'
        f'<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>{notes_list}<p:sldIdLst>{ids}</p:sldIdLst>'
        f'<p:sldSz cx="{_SLIDE_W}" cy="{_SLIDE_H}"/><p:notesSz cx="6858000" cy="9144000"/></p:presentation>')
    parts["ppt/_rels/presentation.xml.rels"] = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="{_REL_NS}">{"".join(pres_rels)}</Relationships>')
    parts["ppt/slideMasters/slideMaster1.xml"] = _MASTER
    parts["ppt/slideMasters/_rels/slideMaster1.xml.rels"] = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="{_REL_NS}">'
        f'<Relationship Id="rId1" Type="{_OD}/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>'
        f'<Relationship Id="rId2" Type="{_OD}/theme" Target="../theme/theme1.xml"/></Relationships>')
    parts["ppt/slideLayouts/slideLayout1.xml"] = _LAYOUT
    parts["ppt/slideLayouts/_rels/slideLayout1.xml.rels"] = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="{_REL_NS}">'
        f'<Relationship Id="rId1" Type="{_OD}/slideMaster" Target="../slideMasters/slideMaster1.xml"/></Relationships>')
    parts["ppt/theme/theme1.xml"] = _THEME
    pml = "application/vnd.openxmlformats-officedocument.presentationml"
    overrides = [("/ppt/presentation.xml", f"{pml}.presentation.main+xml"), ("/ppt/slideMasters/slideMaster1.xml", f"{pml}.slideMaster+xml"),
                 ("/ppt/slideLayouts/slideLayout1.xml", f"{pml}.slideLayout+xml"), ("/ppt/theme/theme1.xml", "application/vnd.openxmlformats-officedocument.theme+xml")]
    overrides += [(f"/ppt/slides/slide{i}.xml", f"{pml}.slide+xml") for i in range(1, count + 1)]
    overrides += [(f"/ppt/notesSlides/notesSlide{i}.xml", f"{pml}.notesSlide+xml") for i in range(1, count + 1) if f"ppt/notesSlides/notesSlide{i}.xml" in parts]
    if has_notes:
        overrides += [("/ppt/notesMasters/notesMaster1.xml", f"{pml}.notesMaster+xml"), ("/ppt/theme/theme2.xml", "application/vnd.openxmlformats-officedocument.theme+xml")]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml",
                    f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="{_CT_NS}">'
                    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                    '<Default Extension="xml" ContentType="application/xml"/>'
                    + "".join(f'<Override PartName="{p}" ContentType="{c}"/>' for p, c in overrides) + "</Types>")
        zf.writestr("_rels/.rels", f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="{_REL_NS}">'
                                   f'<Relationship Id="rId1" Type="{_OD}/officeDocument" Target="ppt/presentation.xml"/></Relationships>')
        for name, data in parts.items():
            zf.writestr(name, data)
    summary = (f"a PowerPoint deck ({count} slide{'s' if count != 1 else ''}"
               + (f", {n_notes} with speaker notes" if n_notes else "") + (f", {n_tables} table{'s' if n_tables != 1 else ''}" if n_tables else "") + ")")
    return buf.getvalue(), summary, ("Warning: " + "; ".join(warnings) + "." if warnings else "")


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
    """A family with all four faces if any is installed (italic must not silently render upright), else the first with a regular."""
    dirs = _font_dirs()
    partial = None
    for family in _FONT_CANDIDATES:
        for d in dirs:
            files = [d / f for f in family]
            if not files[0].is_file():
                continue
            if all(f.is_file() for f in files):
                return tuple(files)
            if partial is None:
                partial = tuple(f if f.is_file() else files[0] for f in files)
    return partial


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
    from reportlab.platypus.doctemplate import LayoutError

    buf = io.BytesIO()
    try:
        SimpleDocTemplate(buf, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=20 * mm, bottomMargin=20 * mm,
                          title="", author="").build(story)
    except LayoutError:
        raise BuildError("a table cell holds more text than fits on one page; put the long text in a paragraph below the "
                         "table and keep the cell short")
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
    """(file bytes, 'a Word document (...)', note) for '.docx' / '.xlsx' / '.pptx' / '.pdf'. Raises BuildError."""
    text = str(content or "")
    if not text.strip():
        raise BuildError("the content is empty")
    if len(text) > MAX_CONTENT_CHARS:
        raise BuildError(f"the content is {len(text):,} characters, over the {MAX_CONTENT_CHARS:,} limit")
    ext = extension.lower()
    if ext not in BUILDABLE_EXTENSIONS:
        raise BuildError(f"{extension} files are not supported")
    text, readback = prepare_text(text, keep_slides=(ext == ".pptx"))
    if not text.strip():
        raise BuildError("the content is empty")
    if ext == ".docx":
        return _build_docx(text)
    if ext == ".xlsx":
        return _build_xlsx(text, readback)
    if ext == ".pdf":
        return _build_pdf(text)
    if ext == ".pptx":
        return _build_pptx(text, readback)
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
