"""
Plain text out of the documents people hand an assistant: PDF, Word, Excel, PowerPoint (PLAN 4.27, reading).

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

DOC_EXTENSIONS = frozenset({".pdf", ".docx", ".xlsx", ".xlsm", ".pptx"})
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
    """'pdf' | 'docx' | 'xlsx' | 'pptx' | 'legacy' | None (not a document: read it as text).

    The extension picks the reader; the first bytes confirm it, so a renamed or damaged file gets a clear
    message instead of a parser crash."""
    ext = path.suffix.lower()
    if ext in LEGACY_EXTENSIONS:
        return "legacy"
    if ext not in DOC_EXTENSIONS:
        return None
    return {".pdf": "pdf", ".docx": "docx", ".xlsx": "xlsx", ".xlsm": "xlsx", ".pptx": "pptx"}[ext]


def _check_magic(path: Path, kind: str) -> None:
    try:
        with open(path, "rb") as f:
            head = f.read(1024)
    except OSError as e:
        raise DocumentError(f"could not open it ({e.strerror or e})")
    if kind == "pdf":
        if b"%PDF-" not in head:
            raise DocumentError("it does not start like a PDF (renamed, damaged or not finished downloading?)")
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
        text = {"pdf": _read_pdf, "docx": _read_docx, "xlsx": _read_xlsx, "pptx": _read_pptx}[kind](path)
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
                lines.extend(_a_paragraphs(tx))
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
