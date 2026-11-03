"""
Plain text out of the documents people hand an assistant: PDF, Word, Excel, PowerPoint, OpenDocument, EPUB, RTF (PLAN 4.27).

``read_file`` calls ``read_document`` before its binary check. Nothing here is imported at startup: ``pypdf``
(pure Python, 4 MB, ~100 ms to import) is imported on the first PDF; DOCX, XLSX and PPTX are zip files of XML, so
they are read with ``zipfile`` and ``xml.etree`` and add no dependency at all. (``python-docx`` + ``python-pptx`` +
``openpyxl`` measured 58 MB with lxml and Pillow, 80-150 ms each, for the same text.)

The text carries its own structure markers (``--- page 3 ---``, ``--- sheet: Budget ---``, ``--- slide 2 ---``) so
``read_file``'s ``start_line`` / ``end_line`` mean something, and a long result is cut to head and tail by the
tool pipeline like any other. Every failure is a sentence the model can pass on, never a traceback.

Limits (not bugs): Excel dates are shown as dates only when the cell's number format is a date format (other formats are
not applied); formulas are shown with the value Excel last saved, which is empty for a file never opened in Excel;
a PDF with no text layer
(a scan) says so and is not OCR'd; legacy ``.doc`` / ``.xls`` / ``.ppt`` are not read.
"""
from __future__ import annotations

import re
import threading
import zipfile
from datetime import datetime, timedelta
from collections import OrderedDict
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET

DOC_EXTENSIONS = frozenset({".pdf", ".docx", ".xlsx", ".xlsm", ".pptx", ".odt", ".ods", ".odp", ".epub", ".rtf"})
LEGACY_EXTENSIONS = {".doc": "Word 97-2003", ".xls": "Excel 97-2003", ".ppt": "PowerPoint 97-2003"}

MAX_FILE_BYTES = 100 * 1024 * 1024          # refuse to even open anything larger
MAX_MEMBER_BYTES = 64 * 1024 * 1024         # one XML part, uncompressed (zip-bomb guard)
MAX_PDF_PAGES = 1000
MAX_TEXT_CHARS = 1_500_000                  # what one read keeps; the pipeline cuts further for the model
MAX_SHEET_ROWS = 2000
MAX_SHEET_COLS = 100

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
_S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"


# What the readers print around a document's text. doc_writers.prepare_text strips exactly these when a read_file result is
# written back (read -> change -> write, or converted: .ods -> .xlsx, .odt/.rtf/.epub -> .docx/.pdf), so the two stay in sync here.
HEADER_RE = re.compile(
    r"^\[(?:Word document|PDF, \d+ pages?|Excel workbook, .*|PowerPoint, .*|OpenDocument (?:text|spreadsheet, .*|presentation, .*)"
    r"|EPUB(?::.*)?, \d+ sections?|RTF document)\]\s*$")
SCAFFOLD_RE = re.compile(
    r"^(?:--- (?:page|section|slide) \d+ ---|\(empty\)|\(no text (?:on this page|on this slide|in it)\)|\(too large to read\)"
    r"|\(row number first, then the columns .*|\(sheet data is missing\)|\(slide data is missing\))\s*$")
CUT_RE = re.compile(r"more (?:rows|pages|sections) not read|\bcut at [\d,]+ characters|characters omitted from the middle"
                    r"|more chars not kept in history")


class DocumentError(Exception):
    """A readable reason, shown to the model as is."""


# ---- cache: a model pages through a big file in several read_file calls ----
_CACHE: "OrderedDict[tuple, str]" = OrderedDict()
_CACHE_MAX = 6
_lock = threading.Lock()


def _cached(path: Path) -> Optional[tuple]:
    try:
        st = path.stat()
    except OSError:
        return None
    return (str(path), st.st_mtime_ns, st.st_size)


# ---- recognising a document ----
def kind_of(path: Path) -> Optional[str]:
    """'pdf' | 'docx' | 'xlsx' | 'pptx' | 'odf' | 'epub' | 'rtf' | 'legacy' | None (not a document: read it as text).

    The extension picks the reader; the first bytes confirm it, so a renamed or damaged file gets a clear
    message instead of a parser crash."""
    ext = path.suffix.lower()
    if ext in LEGACY_EXTENSIONS:
        return "legacy"
    if ext not in DOC_EXTENSIONS:
        return None
    return {".pdf": "pdf", ".docx": "docx", ".xlsx": "xlsx", ".xlsm": "xlsx", ".pptx": "pptx",
            ".odt": "odf", ".ods": "odf", ".odp": "odf", ".epub": "epub", ".rtf": "rtf"}[ext]


def _check_magic(path: Path, kind: str) -> None:
    try:
        with open(path, "rb") as f:
            head = f.read(1024)
    except OSError as e:
        raise DocumentError(f"could not open it ({e.strerror or e})")
    if kind == "pdf":
        if b"%PDF-" not in head:
            raise DocumentError("it does not start like a PDF (renamed, damaged or not finished downloading?)")
    elif kind == "rtf":
        if not head.lstrip().startswith(b"{\\rtf"):
            raise DocumentError("it does not start like an RTF file (renamed or damaged?)")
    elif not head.startswith(b"PK\x03\x04"):
        raise DocumentError(f"it is not a real .{path.suffix.lstrip('.').lower()} file "
                            "(renamed, damaged, or password-protected: those are encrypted, not zipped)")


# ---- entry point ----
def read_document(path: Path, shown_as: str) -> str:
    """The text of a document, or an 'Error: ...' sentence. ``kind_of(path)`` must not be None."""
    kind = kind_of(path)
    if kind == "legacy":
        label = LEGACY_EXTENSIONS[path.suffix.lower()]
        return (f"Error: '{shown_as}' is a {label} file ({path.suffix.lower()}), an older binary format that "
                f"read_file can't open. Ask the user to re-save it as {path.suffix.lower()}x, or as PDF.")
    key = _cached(path)
    with _lock:
        if key and key in _CACHE:
            _CACHE.move_to_end(key)
            return _CACHE[key]
    try:
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise DocumentError(f"it is {size // (1024 * 1024)} MB, over the {MAX_FILE_BYTES // (1024 * 1024)} MB limit")
        _check_magic(path, kind)
        text = {"pdf": _read_pdf, "docx": _read_docx, "xlsx": _read_xlsx, "pptx": _read_pptx,
                "odf": _read_odf, "epub": _read_epub, "rtf": _read_rtf}[kind](path)
    except DocumentError as e:
        return f"Error: could not read '{shown_as}': {e}."
    except (zipfile.BadZipFile, ET.ParseError, KeyError, ValueError, OSError) as e:
        return f"Error: could not read '{shown_as}': the file looks damaged ({type(e).__name__}: {e})."
    except Exception as e:  # a parser bug must not take the turn down
        return f"Error: could not read '{shown_as}': {type(e).__name__}: {e}."
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS] + f"\n[... cut at {MAX_TEXT_CHARS:,} characters ...]"
    with _lock:
        if key:
            _CACHE[key] = text
            while len(_CACHE) > _CACHE_MAX:
                _CACHE.popitem(last=False)
    return text


# ---- PDF ----
def _read_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError:
        raise DocumentError("PDF reading needs the 'pypdf' package, which is not installed in Jarvis's Python")
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted and not reader.decrypt(""):
            raise DocumentError("it is password-protected")
        n = len(reader.pages)
    except DocumentError:
        raise
    except PdfReadError as e:
        raise DocumentError(f"the PDF is damaged ({e})")
    parts = [f"[PDF, {n} page{'s' if n != 1 else ''}]"]
    any_text = False
    shown = min(n, MAX_PDF_PAGES)
    for i in range(shown):
        try:
            page_text = (reader.pages[i].extract_text() or "").strip()
        except Exception:
            page_text = ""
        any_text = any_text or bool(page_text)
        parts.append(f"--- page {i + 1} ---\n{page_text}" if page_text else f"--- page {i + 1} ---\n(no text on this page)")
    if not any_text:
        raise DocumentError("this PDF has no text layer (probably a scan or images only), and Jarvis does not OCR PDFs. "
                            "If it matters, ask the user to attach the pages as images")
    if n > shown:
        parts.append(f"[... {n - shown} more pages not read (limit {MAX_PDF_PAGES}) ...]")
    return "\n".join(parts)


# ---- the OOXML zip helpers ----
def _open_zip(path: Path) -> zipfile.ZipFile:
    return zipfile.ZipFile(path)


def _xml(zf: zipfile.ZipFile, name: str, optional: bool = False) -> Optional[ET.Element]:
    try:
        info = zf.getinfo(name)
    except KeyError:
        if optional:
            return None
        raise DocumentError(f"it is missing '{name}', so it is not a complete Office file")
    if info.file_size > MAX_MEMBER_BYTES:
        raise DocumentError(f"'{name}' inside it is {info.file_size // (1024 * 1024)} MB uncompressed, too large to read")
    data = zf.read(name)
    if b"<!DOCTYPE" in data[:2048].upper() or b"<!ENTITY" in data[:4096].upper():
        raise DocumentError(f"'{name}' declares XML entities, which Jarvis refuses to expand")
    return ET.fromstring(data)


def _rels(zf: zipfile.ZipFile, name: str) -> dict:
    """{Id: Target} of a .rels part ('' when absent)."""
    root = _xml(zf, name, optional=True)
    if root is None:
        return {}
    return {r.get("Id"): r.get("Target") for r in root.iter(f"{_REL}Relationship")}


def _join_target(base_dir: str, target: str) -> str:
    """A relationship target to a zip member name (targets are relative, or absolute from the package root)."""
    if target.startswith("/"):
        return target.lstrip("/")
    parts = base_dir.split("/") if base_dir else []
    for seg in target.split("/"):
        if seg == "..":
            if parts:
                parts.pop()
        elif seg and seg != ".":
            parts.append(seg)
    return "/".join(parts)


# ---- DOCX ----
def _w_text(el: ET.Element) -> str:
    out = []
    for node in el.iter():
        if node.tag == f"{_W}t" and node.text:
            out.append(node.text)
        elif node.tag == f"{_W}tab":
            out.append("\t")
        elif node.tag in (f"{_W}br", f"{_W}cr"):
            out.append("\n")
    return "".join(out)


_HEADING = re.compile(r"^(?:heading|titre|überschrift)\s*(\d)$", re.I)


def _w_paragraph(p: ET.Element) -> str:
    text = _w_text(p).strip()
    if not text:
        return ""
    ppr = p.find(f"{_W}pPr")
    style = ""
    listed = False
    if ppr is not None:
        st = ppr.find(f"{_W}pStyle")
        style = (st.get(f"{_W}val") if st is not None else "") or ""
        listed = ppr.find(f"{_W}numPr") is not None
    norm = style.replace(" ", "")
    m = _HEADING.match(re.sub(r"(?<=\D)(?=\d)", " ", norm))
    if m:
        return "#" * min(6, int(m.group(1))) + " " + text
    if norm.lower() == "title":
        return "# " + text
    if listed or norm.lower().startswith("list"):  # ListBullet, ListNumber, ListParagraph...
        return "- " + text
    return text


def _w_table(tbl: ET.Element) -> list:
    rows = []
    for tr in tbl.findall(f"{_W}tr"):
        cells = []
        for tc in tr.findall(f"{_W}tc"):
            cells.append(" ".join(filter(None, (_w_text(p).strip() for p in tc.iter(f"{_W}p")))))
        rows.append("| " + " | ".join(cells) + " |")
    return rows


def _read_docx(path: Path) -> str:
    with _open_zip(path) as zf:
        root = _xml(zf, "word/document.xml")
    body = root.find(f"{_W}body")
    if body is None:
        raise DocumentError("it has no document body")
    lines = ["[Word document]"]
    for child in body:
        if child.tag == f"{_W}p":
            t = _w_paragraph(child)
            if t:
                lines.append(t)
        elif child.tag == f"{_W}tbl":
            lines.extend(_w_table(child))
        elif child.tag == f"{_W}sdt":  # content controls wrap paragraphs
            content = child.find(f"{_W}sdtContent")
            for p in (content.iter(f"{_W}p") if content is not None else []):
                t = _w_paragraph(p)
                if t:
                    lines.append(t)
    if len(lines) == 1:
        return "[Word document]\n(no text in it)"
    return "\n".join(lines)


# ---- XLSX ----
def _col_index(ref: str) -> int:
    letters = re.match(r"[A-Za-z]+", ref or "")
    n = 0
    for ch in (letters.group(0).upper() if letters else ""):
        n = n * 26 + ord(ch) - 64
    return n - 1


def _col_name(i: int) -> str:
    s, i = "", i + 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _shared_strings(zf: zipfile.ZipFile) -> list:
    root = _xml(zf, "xl/sharedStrings.xml", optional=True)
    if root is None:
        return []
    out = []
    for si in root.iter(f"{_S}si"):
        out.append("".join(t.text or "" for t in si.iter(f"{_S}t")))
    return out


_BUILTIN_DATE_FORMATS = frozenset(list(range(14, 23)) + list(range(27, 37)) + list(range(45, 48)) + list(range(50, 59)))
_BUILTIN_TIME_ONLY = frozenset({18, 19, 20, 21, 45, 46, 47})
_QUOTED_OR_BRACKETED = re.compile(r'"[^"]*"|\[[^\]]*\]|\\.|_.|\*.')
_DATE_TOKENS = re.compile(r"[ymdhs]", re.I)
_DAY_PART = re.compile(r"[ydg]", re.I)  # a year or a day: the format shows a date, not only a time


def _custom_kind(code: str) -> Optional[str]:
    """'date' | 'time' for a custom number format that shows one (d, m, y, h, s outside quotes, brackets, escapes)."""
    bare = _QUOTED_OR_BRACKETED.sub("", code or "")
    if not _DATE_TOKENS.search(bare):
        return None
    return "date" if _DAY_PART.search(bare) else ("time" if re.search(r"[hs]", bare, re.I) else "date")


def _date_styles(zf: zipfile.ZipFile) -> dict:
    """{cell style index (the c@s attribute): 'date' | 'time'} for styles whose number format is a date or time."""
    root = _xml(zf, "xl/styles.xml", optional=True)
    if root is None:
        return {}
    custom = {n.get("numFmtId"): n.get("formatCode") or "" for n in root.iter(f"{_S}numFmt")}
    xfs = root.find(f"{_S}cellXfs")
    out = {}
    for i, xf in enumerate(xfs.findall(f"{_S}xf") if xfs is not None else []):
        fid = xf.get("numFmtId", "0")
        if fid in custom:
            kind = _custom_kind(custom[fid])
        elif fid.isdigit() and int(fid) in _BUILTIN_DATE_FORMATS:
            kind = "time" if int(fid) in _BUILTIN_TIME_ONLY else "date"
        else:
            kind = None
        if kind:
            out[str(i)] = kind
    return out


def _serial_to_text(value: str, kind: str, date1904: bool) -> str:
    """'46298' -> '2026-10-03'; '46298.5' -> '2026-10-03 12:00'; a time format on '0.75' -> '18:00'.
    The value unchanged if it isn't a plausible serial."""
    try:
        serial = float(value)
    except ValueError:
        return value
    if serial < 0 or serial > 2_958_465:  # past 9999-12-31
        return value
    if kind == "time":
        seconds = round((serial % 1) * 86400)
        h, rest = divmod(seconds, 3600)
        m, sec = divmod(rest, 60)
        return f"{h % 24:02d}:{m:02d}" + (f":{sec:02d}" if sec else "")
    if date1904:
        base = datetime(1904, 1, 1)
    elif serial < 1:
        return value  # Excel's "day 0" (1/0/1900) has no calendar date
    else:
        base = datetime(1899, 12, 30)  # absorbs Excel's phantom 1900-02-29 for every date after it
        if serial < 61:
            base = datetime(1899, 12, 31)  # 1 Jan 1900 .. 28 Feb 1900 sit before the phantom day
    moment = base + timedelta(seconds=round(serial * 86400))
    if moment.hour == 0 and moment.minute == 0 and moment.second == 0:
        return f"{moment:%Y-%m-%d}"
    return f"{moment:%Y-%m-%d %H:%M}" + (f":{moment:%S}" if moment.second else "")


def _cell_text(c: ET.Element, shared: list, dates: Optional[dict] = None, date1904: bool = False) -> str:
    t = c.get("t", "n")
    v = c.find(f"{_S}v")
    f = c.find(f"{_S}f")
    if t == "inlineStr":
        value = "".join(x.text or "" for x in c.iter(f"{_S}t"))
    elif v is None or v.text is None:
        value = ""
    elif t == "s":
        try:
            value = shared[int(v.text)]
        except (ValueError, IndexError):
            value = ""
    elif t == "b":
        value = "TRUE" if v.text.strip() == "1" else "FALSE"
    elif t in ("n", "") and dates and c.get("s") in dates:
        value = _serial_to_text(v.text.strip(), dates[c.get("s")], date1904)
    else:
        value = v.text
    value = value.replace("\t", " ").replace("\r", " ").replace("\n", " ")
    if f is not None:
        formula = (f.text or "").strip()
        if formula:
            return f"={formula}" + (f" -> {value}" if value != "" else "")
        return value  # a shared-formula follower: no text of its own, the saved value is the result
    return value


def _read_xlsx(path: Path) -> str:
    with _open_zip(path) as zf:
        wb = _xml(zf, "xl/workbook.xml")
        rels = _rels(zf, "xl/_rels/workbook.xml.rels")
        shared = _shared_strings(zf)
        dates = _date_styles(zf)
        pr = wb.find(f"{_S}workbookPr")
        date1904 = pr is not None and (pr.get("date1904") or "").lower() in ("1", "true")
        sheets = []
        for s in wb.iter(f"{_S}sheet"):
            target = rels.get(s.get(f"{_R}id"))
            if target:
                sheets.append((s.get("name") or "Sheet", _join_target("xl", target), s.get("state") or ""))
        if not sheets:
            raise DocumentError("the workbook has no sheets")
        out = [f"[Excel workbook, {len(sheets)} sheet{'s' if len(sheets) != 1 else ''}]"]
        for name, member, state in sheets:
            hidden = " (hidden)" if state in ("hidden", "veryHidden") else ""
            out.append(f"--- sheet: {name}{hidden} ---")
            root = _xml(zf, member, optional=True)
            if root is None:
                out.append("(sheet data is missing)")
                continue
            data = root.find(f"{_S}sheetData")
            rows = list(data.iter(f"{_S}row")) if data is not None else []
            if not rows:
                out.append("(empty)")
                continue
            shown = 0
            for row in rows:
                if shown >= MAX_SHEET_ROWS:
                    out.append(f"[... {len(rows) - shown} more rows not read (limit {MAX_SHEET_ROWS} per sheet) ...]")
                    break
                cells = {}
                for c in row.findall(f"{_S}c"):
                    idx = _col_index(c.get("r", ""))
                    if 0 <= idx < MAX_SHEET_COLS:
                        text = _cell_text(c, shared, dates, date1904)
                        if text != "":
                            cells[idx] = text
                if not cells:
                    continue
                last = max(cells)
                line = "\t".join(cells.get(i, "") for i in range(last + 1))
                out.append(f"{row.get('r', '?')}: {line}")
                shown += 1
            if shown == 0:
                out.append("(empty)")
        out.append("(row number first, then the columns A, B, C... separated by tabs; "
                   "a formula reads =FORMULA -> last saved value)")
        return "\n".join(out)


# ---- PPTX ----
def _a_paragraphs(el: ET.Element) -> list:
    out = []
    for p in el.iter(f"{_A}p"):
        t = "".join(x.text or "" for x in p.iter(f"{_A}t")).strip()
        if t:
            out.append(t)
    return out


def _shape_text(tree: ET.Element) -> list:
    """Text of a slide's shapes in document order; tables as pipe rows."""
    lines = []
    for el in tree.iter():
        if el.tag == f"{_P}sp":
            tx = el.find(f"{_P}txBody")
            if tx is not None:
                ph = el.find(f"{_P}nvSpPr/{_P}nvPr/{_P}ph")
                bulleted = ph is not None and ph.get("type", "obj") in ("body", "obj")   # the layout bullets these paragraphs
                for p in tx.findall(f"{_A}p"):
                    text = "".join(x.text or "" for x in p.iter(f"{_A}t")).strip()
                    if text:
                        no_bullet = p.find(f"{_A}pPr/{_A}buNone") is not None
                        lines.append(f"- {text}" if bulleted and not no_bullet else text)
        elif el.tag == f"{_A}tbl":
            for tr in el.findall(f"{_A}tr"):
                cells = [" ".join(_a_paragraphs(tc)) for tc in tr.findall(f"{_A}tc")]
                lines.append("| " + " | ".join(cells) + " |")
    return lines


def _read_pptx(path: Path) -> str:
    with _open_zip(path) as zf:
        pres = _xml(zf, "ppt/presentation.xml")
        rels = _rels(zf, "ppt/_rels/presentation.xml.rels")
        order = []
        for sld in pres.iter(f"{_P}sldId"):
            target = rels.get(sld.get(f"{_R}id"))
            if target:
                order.append(_join_target("ppt", target))
        if not order:
            raise DocumentError("the presentation has no slides")
        out = [f"[PowerPoint, {len(order)} slide{'s' if len(order) != 1 else ''}]"]
        for n, member in enumerate(order, 1):
            slide = _xml(zf, member, optional=True)
            out.append(f"--- slide {n} ---")
            if slide is None:
                out.append("(slide data is missing)")
                continue
            text = _shape_text(slide)
            out.extend(text or ["(no text on this slide)"])
            base, _, fname = member.rpartition("/")
            for target in _rels(zf, f"{base}/_rels/{fname}.rels").values():
                if target and "notesSlide" in target:
                    notes = _xml(zf, _join_target(base, target), optional=True)
                    if notes is not None:
                        # a notes page also holds the slide image and the slide number: only the body placeholder
                        body = [t for sp in notes.iter(f"{_P}sp") for t in _notes_body(sp)]
                        if body:
                            out.append("Notes: " + " ".join(body))
        return "\n".join(out)


def _notes_body(sp: ET.Element) -> list:
    ph = sp.find(f"{_P}nvSpPr/{_P}nvPr/{_P}ph")
    if ph is None or ph.get("type") != "body":
        return []
    tx = sp.find(f"{_P}txBody")
    return _a_paragraphs(tx) if tx is not None else []


# ---- OpenDocument (.odt, .ods, .odp) ----
_OT = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
_OTB = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"
_OO = "{urn:oasis:names:tc:opendocument:xmlns:office:1.0}"
_OD_ = "{urn:oasis:names:tc:opendocument:xmlns:drawing:1.0}"
_OP = "{urn:oasis:names:tc:opendocument:xmlns:presentation:1.0}"


def _o_text(el: ET.Element) -> str:
    out = [el.text or ""]
    for ch in el:
        if ch.tag == f"{_OT}s":
            out.append(" " * min(200, int(ch.get(f"{_OT}c", "1") or 1)))
        elif ch.tag == f"{_OT}tab":
            out.append("\t")
        elif ch.tag == f"{_OT}line-break":
            out.append("\n")
        elif ch.tag != f"{_OT}note":  # footnote bodies are not part of the running text
            out.append(_o_text(ch))
        out.append(ch.tail or "")
    return "".join(out)


def _o_table_rows(tbl: ET.Element) -> list:
    rows = []
    for tr in tbl.iter(f"{_OTB}table-row"):
        cells = []
        for tc in tr:
            if tc.tag not in (f"{_OTB}table-cell", f"{_OTB}covered-table-cell"):
                continue
            text = " ".join(filter(None, (_o_text(p).strip() for p in tc.iter(f"{_OT}p"))))
            cells.extend([text] * min(50, int(tc.get(f"{_OTB}number-columns-repeated", "1") or 1)))
        while cells and cells[-1] == "":
            cells.pop()
        if cells:
            rows.append("| " + " | ".join(cells) + " |")
    return rows


def _o_blocks(parent: ET.Element, lines: list, listed: bool = False) -> None:
    for el in parent:
        tag = el.tag
        if tag == f"{_OT}h":
            t = _o_text(el).strip()
            if t:
                lines.append("#" * max(1, min(6, int(el.get(f"{_OT}outline-level", "1") or 1))) + " " + t)
        elif tag == f"{_OT}p":
            t = _o_text(el).strip()
            if t:
                lines.append(("- " if listed else "") + t)
        elif tag == f"{_OT}list":
            for item in el.findall(f"{_OT}list-item"):
                _o_blocks(item, lines, listed=True)  # nested lists recurse through here
        elif tag == f"{_OTB}table":
            lines.extend(_o_table_rows(el))
        elif tag in (f"{_OT}section", f"{_OT}table-of-content", f"{_OT}index-body"):
            _o_blocks(el, lines, listed)


def _o_cell(tc: ET.Element) -> str:
    vtype = tc.get(f"{_OO}value-type", "")
    text = " ".join(filter(None, (_o_text(p).strip() for p in tc.iter(f"{_OT}p"))))
    if vtype == "date":
        v = tc.get(f"{_OO}date-value", "")
        if "T" in v:
            day, _, clock = v.partition("T")
            clock = clock[:8]
            v = day if clock.startswith("00:00") and clock[5:] in ("", ":00") else f"{day} {clock[:5]}" + (clock[5:] if clock[5:] not in ("", ":00") else "")
        return v or text
    if vtype == "time":
        m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?", tc.get(f"{_OO}time-value", ""))
        if m:
            return f"{int(m.group(1) or 0):02d}:{int(m.group(2) or 0):02d}"
        return text
    if vtype == "boolean":
        return "TRUE" if tc.get(f"{_OO}boolean-value") == "true" else "FALSE"
    if vtype in ("float", "percentage", "currency"):
        return tc.get(f"{_OO}value") or text
    if vtype == "string" and tc.get(f"{_OO}string-value") is not None:
        return tc.get(f"{_OO}string-value", "")
    return text


def _o_formula(f: str) -> str:
    """'of:=SUM([.B2:.B4])' -> '=SUM(B2:B4)'; '[$Sheet1.B6]' -> 'Sheet1!B6'."""
    f = re.sub(r"^(?:of|oooc):", "", f)
    f = re.sub(r"\[\$?(?:('[^']+')|([A-Za-z_][\w]*))\.(\$?[A-Za-z]+\$?\d+)",
               lambda m: f"[{m.group(1) or m.group(2)}!{m.group(3)}", f)  # sheet-qualified; a quoted name ('By category') keeps its quotes
    f = f.replace("[.", "[").replace(":.", ":")
    return f.replace("[", "").replace("]", "").replace(";", ",")  # OpenFormula separates arguments with ';', Excel with ','



def _read_ods_body(spreadsheet: ET.Element) -> list:
    tables = list(spreadsheet.iter(f"{_OTB}table"))
    out = [f"[OpenDocument spreadsheet, {len(tables)} sheet{'s' if len(tables) != 1 else ''}]"]
    for tbl in tables:
        out.append(f"--- sheet: {tbl.get(f'{_OTB}name', 'Sheet')} ---")
        r = 0
        shown = 0
        any_row = False
        for tr in tbl.iter(f"{_OTB}table-row"):
            repeat = max(1, int(tr.get(f"{_OTB}number-rows-repeated", "1") or 1))
            cells, col = {}, 0
            for tc in tr:
                if tc.tag not in (f"{_OTB}table-cell", f"{_OTB}covered-table-cell"):
                    continue
                crep = max(1, int(tc.get(f"{_OTB}number-columns-repeated", "1") or 1))
                value = _o_cell(tc).replace("\t", " ").replace("\r", " ").replace("\n", " ")
                formula = tc.get(f"{_OTB}formula")
                if formula:
                    text = _o_formula(formula)
                    value = text + (f" -> {value}" if value != "" else "")
                if value != "":
                    for k in range(min(crep, 20)):
                        if col + k < MAX_SHEET_COLS:
                            cells[col + k] = value
                col += crep
            if not cells:
                r += repeat
                continue
            for k in range(min(repeat, 50)):
                if shown >= MAX_SHEET_ROWS:
                    break
                last = max(cells)
                out.append(f"{r + 1 + k}: " + "\t".join(cells.get(i, "") for i in range(last + 1)))
                shown += 1
                any_row = True
            r += repeat
            if shown >= MAX_SHEET_ROWS:
                out.append(f"[... more rows not read (limit {MAX_SHEET_ROWS} per sheet) ...]")
                break
        if not any_row:
            out.append("(empty)")
    out.append("(row number first, then the columns A, B, C... separated by tabs; "
               "a formula reads =FORMULA -> last saved value)")
    return out


_ODP_PLACEHOLDERS = frozenset({"page-number", "footer", "date-time", "header", "handout", "slide-number"})


def _odp_paragraphs(el: ET.Element, keep_class: Optional[str]) -> list:
    """Paragraphs under ``el``. On a slide, frames that are page-number / footer / date placeholders are skipped; on a notes
    page only the frame of class ``notes`` counts (the page also holds the slide image and the slide number field)."""
    out = []
    cls = el.get(f"{_OP}class")
    if keep_class is None and cls in _ODP_PLACEHOLDERS:
        return out
    if keep_class is not None and cls is not None and cls != keep_class and el.tag == f"{_OD_}frame":
        return out
    if el.tag == f"{_OTB}table":
        return _o_table_rows(el)
    if el.tag in (f"{_OT}p", f"{_OT}h"):
        t = _o_text(el).strip()
        return [t] if t else out
    for ch in el:
        out.extend(_odp_paragraphs(ch, keep_class))
    return out


def _read_odp_body(presentation: ET.Element) -> list:
    pages = list(presentation.iter(f"{_OD_}page"))
    out = [f"[OpenDocument presentation, {len(pages)} slide{'s' if len(pages) != 1 else ''}]"]
    for n, page in enumerate(pages, 1):
        out.append(f"--- slide {n} ---")
        text, notes = [], []
        for child in page:
            if child.tag == f"{_OP}notes":
                notes.extend(_odp_paragraphs(child, "notes"))
            else:
                text.extend(_odp_paragraphs(child, None))
        out.extend(text or ["(no text on this slide)"])
        if notes:
            out.append("Notes: " + " ".join(notes))
    return out


def _read_odf(path: Path) -> str:
    with _open_zip(path) as zf:
        root = _xml(zf, "content.xml")
    body = root.find(f"{_OO}body")
    if body is None:
        raise DocumentError("it has no document body")
    text = body.find(f"{_OO}text")
    if text is not None:
        lines = ["[OpenDocument text]"]
        _o_blocks(text, lines)
        return "\n".join(lines) if len(lines) > 1 else "[OpenDocument text]\n(no text in it)"
    sheet = body.find(f"{_OO}spreadsheet")
    if sheet is not None:
        return "\n".join(_read_ods_body(sheet))
    pres = body.find(f"{_OO}presentation")
    if pres is not None:
        return "\n".join(_read_odp_body(pres))
    raise DocumentError("it is an OpenDocument file of a kind Jarvis does not read (a drawing or database?)")


# ---- EPUB ----
from html.parser import HTMLParser  # noqa: E402  (stdlib; kept next to its only user)
from urllib.parse import unquote  # noqa: E402

_EPUB_SKIP = frozenset({"script", "style", "head", "svg", "nav"})
_EPUB_BLOCK = frozenset({"p", "div", "br", "li", "tr", "section", "article", "blockquote", "pre", "ul", "ol", "table", "hr", "figure"})


class _XhtmlText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list = []
        self._skip = 0
        self._cell = 0          # inside a table cell: paragraph breaks become spaces so a row stays one line
        self._first_cell = True

    def handle_starttag(self, tag, attrs):
        if tag in _EPUB_SKIP:
            self._skip += 1
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.parts.append("\n" + "#" * int(tag[1]) + " ")
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag == "tr":
            self.parts.append("\n| ")
            self._first_cell = True
        elif tag in ("td", "th"):
            if not self._first_cell:
                self.parts.append(" | ")
            self._first_cell = False
            self._cell += 1
        elif tag in _EPUB_BLOCK:
            self.parts.append(" " if self._cell else "\n")

    def handle_endtag(self, tag):
        if tag in _EPUB_SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in ("td", "th"):
            self._cell = max(0, self._cell - 1)
        elif tag == "tr":
            self.parts.append(" |\n")
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6") or tag in _EPUB_BLOCK:
            self.parts.append(" " if self._cell else "\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        lines = (re.sub(r"[ \t\r\f\v]+", " ", l).strip() for l in raw.split("\n"))
        return "\n".join(l for l in lines if l and l != "|")


_DC = "{http://purl.org/dc/elements/1.1/}"
_OPF = "{http://www.idpf.org/2007/opf}"
_CONTAINER = "{urn:oasis:names:tc:opendocument:xmlns:container}"


def _read_epub(path: Path) -> str:
    with _open_zip(path) as zf:
        container = _xml(zf, "META-INF/container.xml")
        rf = container.find(f".//{_CONTAINER}rootfile")
        if rf is None or not rf.get("full-path"):
            raise DocumentError("it has no package file (not a valid EPUB)")
        opf_path = rf.get("full-path")
        opf = _xml(zf, opf_path)
        base = opf_path.rpartition("/")[0]
        manifest = {i.get("id"): i.get("href") for i in opf.iter(f"{_OPF}item") if i.get("href")}
        spine = [manifest[r.get("idref")] for r in opf.iter(f"{_OPF}itemref") if r.get("idref") in manifest]
        if not spine:
            raise DocumentError("the book has no readable chapters")
        title = (opf.findtext(f".//{_DC}title") or "").strip()
        author = (opf.findtext(f".//{_DC}creator") or "").strip()
        head = "[EPUB" + (f": {title}" if title else "") + (f" by {author}" if author else "") + f", {len(spine)} sections]"
        out, total = [head], 0
        for n, href in enumerate(spine, 1):
            member = _join_target(base, unquote(href.split("#")[0]))
            try:
                info = zf.getinfo(member)
            except KeyError:
                continue
            if info.file_size > MAX_MEMBER_BYTES:
                out.append(f"--- section {n} ---\n(too large to read)")
                continue
            parser = _XhtmlText()
            parser.feed(zf.read(member).decode("utf-8", errors="replace"))
            text = parser.text()
            if not text:
                continue
            out.append(f"--- section {n} ---\n{text}")
            total += len(text)
            if total > MAX_TEXT_CHARS:
                out.append(f"[... more sections not read (cut at {MAX_TEXT_CHARS:,} characters) ...]")
                break
        if len(out) == 1:
            raise DocumentError("no text was found in the book")
        return "\n".join(out)


# ---- RTF ----
_RTF_SKIP_DESTINATIONS = frozenset({
    "fonttbl", "colortbl", "stylesheet", "info", "pict", "header", "footer", "headerl", "headerr", "headerf", "footerl",
    "footerr", "footerf", "themedata", "datastore", "listtable", "listoverridetable", "rsidtbl", "generator", "latentstyles",
    "xmlnstbl", "fldinst", "objdata", "colorschememapping", "fchars", "lchars", "private", "revtbl", "bkmkstart", "bkmkend",
})
_RTF_SYMBOLS = {"bullet": "\u2022", "emdash": "\u2014", "endash": "\u2013", "lquote": "\u2018", "rquote": "\u2019",
                "ldblquote": "\u201c", "rdblquote": "\u201d", "emspace": " ", "enspace": " ", "tab": "\t", "cell": "\t",
                "nestcell": "\t", "row": "\n", "nestrow": "\n", "par": "\n", "line": "\n", "page": "\n", "sect": "\n"}
_RTF_WORD = re.compile(r"\\([a-zA-Z]+)(-?\d+)? ?")


def _read_rtf(path: Path) -> str:
    data = path.read_bytes().decode("latin-1")
    out: list = []
    stack: list = []          # (skipping, unicode fallback length) per open group
    skipping, uc = False, 1
    codepage = "cp1252"
    i, n = 0, len(data)
    skip_chars = 0            # fallback characters after a \uN to drop
    while i < n:
        c = data[i]
        if c == "{":
            stack.append((skipping, uc))
            i += 1
        elif c == "}":
            if stack:
                skipping, uc = stack.pop()
            i += 1
        elif c == "\\":
            nxt = data[i + 1] if i + 1 < n else ""
            if nxt in "\\{}":
                if not skipping and skip_chars == 0:
                    out.append(nxt)
                skip_chars = max(0, skip_chars - 1)
                i += 2
            elif nxt == "'":
                hexv = data[i + 2:i + 4]
                i += 4
                if skip_chars:
                    skip_chars -= 1
                elif not skipping:
                    try:
                        out.append(bytes([int(hexv, 16)]).decode(codepage, errors="replace"))
                    except ValueError:
                        pass
            elif nxt == "*":
                skipping = True   # an ignorable destination: {\*\something ...}
                i += 2
            elif nxt == "~":
                out.append("\u00a0") if not skipping else None
                i += 2
            elif nxt == "_":
                out.append("-") if not skipping else None
                i += 2
            elif nxt in "-\n\r":
                i += 2
            else:
                m = _RTF_WORD.match(data, i)
                if not m:
                    i += 2
                    continue
                word, param = m.group(1), m.group(2)
                i = m.end()
                if word in _RTF_SKIP_DESTINATIONS:
                    skipping = True
                elif word == "uc" and param is not None:
                    uc = max(0, int(param))
                elif word == "ansicpg" and param is not None:
                    codepage = f"cp{param}"
                elif word == "u" and param is not None and not skipping:
                    code = int(param)
                    out.append(chr(code + 65536 if code < 0 else code))
                    skip_chars = uc
                elif word in _RTF_SYMBOLS and not skipping:
                    out.append(_RTF_SYMBOLS[word])
        else:
            if skip_chars:
                skip_chars -= 1
            elif not skipping and c not in "\r\n":
                out.append(c)
            i += 1
    text = "".join(out)
    lines = [re.sub(r"[ \t]+$", "", l) for l in text.split("\n")]
    lines = [re.sub(r"^[ \t]+|\t+$", "", l) for l in lines]
    body = "\n".join(l for l in lines if l.strip())
    if not body.strip():
        raise DocumentError("no text was found in it")
    return "[RTF document]\n" + body
