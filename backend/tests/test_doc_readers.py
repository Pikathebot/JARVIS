"""read_file on PDF, Word, Excel and PowerPoint files (PLAN 4.27, reading).

The documents are built here from raw zip/XML (and a hand-made PDF), so the tests need none of the
python-docx / openpyxl / python-pptx libraries and no binary fixtures in the repo.
"""
import zipfile
from pathlib import Path

import pytest

from app.agent.tools import doc_readers
from app.agent.tools.read_file import read_file

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
S = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
R = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
A = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
P = 'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
REL = "http://schemas.openxmlformats.org/package/2006/relationships"


def _zip(path: Path, parts: dict) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in parts.items():
            zf.writestr(name, data)
    return path


def _rels(*pairs) -> str:
    items = "".join(f'<Relationship Id="{i}" Type="x" Target="{t}"/>' for i, t in pairs)
    return f'<?xml version="1.0"?><Relationships xmlns="{REL}">{items}</Relationships>'


@pytest.fixture(autouse=True)
def _fresh_cache():
    doc_readers._CACHE.clear()
    yield
    doc_readers._CACHE.clear()


@pytest.fixture
def ws(tmp_path):
    return tmp_path


def _read(ws: Path, name: str, **kw):
    return read_file(name, workspace_path=str(ws), **kw)


# ---------------- DOCX ----------------
def _docx(path: Path) -> Path:
    body = (
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Quarterly report</w:t></w:r></w:p>'
        '<w:p><w:r><w:t xml:space="preserve">Revenue grew </w:t></w:r><w:r><w:t>12%.</w:t></w:r></w:p>'
        '<w:p><w:pPr><w:pStyle w:val="Heading2"/></w:pPr><w:r><w:t>Risks</w:t></w:r></w:p>'
        '<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr><w:r><w:t>Supply</w:t></w:r></w:p>'
        '<w:p><w:pPr><w:pStyle w:val="ListBullet"/></w:pPr><w:r><w:t>Word built-in bullet</w:t></w:r></w:p>'
        '<w:p/>'
        '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Region</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Sales</w:t></w:r></w:p></w:tc></w:tr>'
        '<w:tr><w:tc><w:p><w:r><w:t>North</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>40</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
        '<w:sdt><w:sdtContent><w:p><w:r><w:t>Inside a content control</w:t></w:r></w:p></w:sdtContent></w:sdt>'
    )
    return _zip(path, {"word/document.xml": f'<?xml version="1.0"?><w:document {W}><w:body>{body}</w:body></w:document>'})


def test_docx_text_headings_lists_and_tables(ws):
    _docx(ws / "report.docx")
    out = _read(ws, "report.docx")
    lines = out.splitlines()
    assert lines[0] == "[Word document]"
    assert "# Quarterly report" in lines and "## Risks" in lines
    assert "Revenue grew 12%." in lines
    assert "- Supply" in lines
    assert "- Word built-in bullet" in lines  # style id ListBullet, no numPr (seen in a python-docx file)
    assert "| Region | Sales |" in lines and "| North | 40 |" in lines
    assert "Inside a content control" in lines


def test_a_line_range_applies_to_the_extracted_text(ws):
    _docx(ws / "report.docx")
    out = _read(ws, "report.docx", start_line=2, end_line=3)
    assert out.startswith("[Lines 2-3 of ") and "# Quarterly report" in out and "Revenue grew 12%." in out
    assert "Risks" not in out


def test_an_empty_docx_says_so(ws):
    _zip(ws / "e.docx", {"word/document.xml": f'<w:document {W}><w:body><w:p/></w:body></w:document>'})
    assert "(no text in it)" in _read(ws, "e.docx")


# ---------------- XLSX ----------------
def _xlsx(path: Path) -> Path:
    sheet1 = (
        f'<worksheet {S}><sheetData>'
        '<row r="1"><c r="A1" t="s"><v>Item</v></c><c r="B1" t="s"><v>Cost</v></c><c r="D1" t="inlineStr"><is><t>Note</t></is></c></row>'
        '<row r="2"><c r="A2" t="s"><v>Pens</v></c><c r="B2"><v>3.5</v></c></row>'
        '<row r="3"><c r="A3" t="s"><v>Total</v></c><c r="B3"><f>SUM(B2:B2)</f><v>3.5</v></c><c r="C3" t="b"><v>1</v></c></row>'
        '<row r="5"><c r="A5"><f>NOW()</f></c></row>'
        '</sheetData></worksheet>'
    )
    sheet2 = f'<worksheet {S}><sheetData/></worksheet>'
    wb = (f'<workbook {S} {R}><sheets><sheet name="Budget" sheetId="1" r:id="rId1"/>'
          '<sheet name="Old" sheetId="2" state="hidden" r:id="rId2"/></sheets></workbook>')
    # values are looked up by the Id, not the order of the parts in the zip
    sst = f'<sst {S}><si><t>Item</t></si><si><t>Cost</t></si><si><t>Pens</t></si><si><t>Total</t></si></sst>'
    # A1 uses index 0 ("Item"), B1 1, A2 2, A3 3 -- rewrite the indices to that table
    sheet1 = (sheet1.replace('<c r="A1" t="s"><v>Item</v>', '<c r="A1" t="s"><v>0</v>')
              .replace('<c r="B1" t="s"><v>Cost</v>', '<c r="B1" t="s"><v>1</v>')
              .replace('<c r="A2" t="s"><v>Pens</v>', '<c r="A2" t="s"><v>2</v>')
              .replace('<c r="A3" t="s"><v>Total</v>', '<c r="A3" t="s"><v>3</v>'))
    return _zip(path, {
        "xl/workbook.xml": wb,
        "xl/_rels/workbook.xml.rels": _rels(("rId2", "worksheets/sheet2.xml"), ("rId1", "worksheets/sheet1.xml")),
        "xl/sharedStrings.xml": sst,
        "xl/worksheets/sheet1.xml": sheet1,
        "xl/worksheets/sheet2.xml": sheet2,
    })


def test_xlsx_sheets_rows_formulas_and_gaps(ws):
    _xlsx(ws / "b.xlsx")
    out = _read(ws, "b.xlsx")
    assert out.startswith("[Excel workbook, 2 sheets]")
    assert "--- sheet: Budget ---" in out and "--- sheet: Old (hidden) ---" in out
    assert "1: Item\tCost\t\tNote" in out            # column C empty, D kept in place
    assert "2: Pens\t3.5" in out
    assert "3: Total\t=SUM(B2:B2) -> 3.5\tTRUE" in out
    assert "5: =NOW()" in out                          # a formula never calculated has no saved value
    assert "(empty)" in out                            # the hidden sheet


def test_xlsx_caps_rows_per_sheet(ws, monkeypatch):
    monkeypatch.setattr(doc_readers, "MAX_SHEET_ROWS", 3)
    rows = "".join(f'<row r="{i}"><c r="A{i}"><v>{i}</v></c></row>' for i in range(1, 11))
    _zip(ws / "big.xlsx", {
        "xl/workbook.xml": f'<workbook {S} {R}><sheets><sheet name="S" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": _rels(("rId1", "worksheets/sheet1.xml")),
        "xl/worksheets/sheet1.xml": f'<worksheet {S}><sheetData>{rows}</sheetData></worksheet>',
    })
    out = _read(ws, "big.xlsx")
    assert "3: 3" in out and "4: 4" not in out
    assert "7 more rows not read" in out


# ---------------- PPTX ----------------
def _pptx(path: Path) -> Path:
    def slide(title, body):
        return (f'<p:sld {P} {A}><p:cSld><p:spTree>'
                f'<p:sp><p:txBody><a:p><a:r><a:t>{title}</a:t></a:r></a:p></p:txBody></p:sp>'
                f'<p:sp><p:txBody><a:p><a:r><a:t>{body}</a:t></a:r></a:p></p:txBody></p:sp>'
                '</p:spTree></p:cSld></p:sld>')
    notes = (f'<p:notes {P} {A}><p:cSld><p:spTree>'
             '<p:sp><p:nvSpPr><p:cNvPr id="1" name="img"/><p:cNvSpPr/><p:nvPr><p:ph type="sldImg"/></p:nvPr></p:nvSpPr></p:sp>'
             '<p:sp><p:nvSpPr><p:cNvPr id="2" name="n"/><p:cNvSpPr/><p:nvPr><p:ph type="body"/></p:nvPr></p:nvSpPr>'
             '<p:txBody><a:p><a:r><a:t>Say this first</a:t></a:r></a:p></p:txBody></p:sp>'
             '<p:sp><p:nvSpPr><p:cNvPr id="3" name="num"/><p:cNvSpPr/><p:nvPr><p:ph type="sldNum"/></p:nvPr></p:nvSpPr>'
             '<p:txBody><a:p><a:r><a:t>7</a:t></a:r></a:p></p:txBody></p:sp>'
             '</p:spTree></p:cSld></p:notes>')
    # presentation order is slide2 then slide1: the order of ids, not of the file names
    pres = (f'<p:presentation {P} {R}><p:sldIdLst><p:sldId id="256" r:id="rId2"/><p:sldId id="257" r:id="rId1"/>'
            '</p:sldIdLst></p:presentation>')
    return _zip(path, {
        "ppt/presentation.xml": pres,
        "ppt/_rels/presentation.xml.rels": _rels(("rId1", "slides/slide1.xml"), ("rId2", "slides/slide2.xml")),
        "ppt/slides/slide1.xml": slide("Closing", "Questions?"),
        "ppt/slides/slide2.xml": slide("Welcome", "Agenda"),
        "ppt/slides/_rels/slide2.xml.rels": _rels(("rId1", "../notesSlides/notesSlide2.xml")),
        "ppt/notesSlides/notesSlide2.xml": notes,
    })


def test_pptx_slides_in_presentation_order_with_notes(ws):
    _pptx(ws / "deck.pptx")
    out = _read(ws, "deck.pptx")
    assert out.startswith("[PowerPoint, 2 slides]")
    assert out.index("--- slide 1 ---") < out.index("Welcome") < out.index("--- slide 2 ---") < out.index("Closing")
    assert "Notes: Say this first" in out
    assert "Notes: Say this first 7" not in out        # the slide number placeholder is not a note


# ---------------- PDF ----------------
def _pdf(path: Path, pages: list) -> Path:
    """A tiny valid PDF; each page is a string drawn with Helvetica ('' = a blank page)."""
    objs = {}
    kids = []
    n = 3
    for text in pages:
        page, content = n, n + 1
        kids.append(f"{page} 0 R")
        stream = f"BT /F1 12 Tf 72 700 Td ({text}) Tj ET" if text else ""
        objs[page] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content} 0 R "
                      "/Resources << /Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >> >> >>")
        objs[content] = f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream"
        n += 2
    objs[1] = "<< /Type /Catalog /Pages 2 0 R >>"
    objs[2] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(pages)} >>"
    out = b"%PDF-1.4\n"
    offsets = {}
    for num in sorted(objs):
        offsets[num] = len(out)
        out += f"{num} 0 obj\n{objs[num]}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for num in sorted(objs):
        out += f"{offsets[num]:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(out)
    return path


def test_pdf_text_by_page(ws):
    _pdf(ws / "p.pdf", ["Hello first page", "Second page here"])
    out = _read(ws, "p.pdf")
    assert out.startswith("[PDF, 2 pages]")
    assert "--- page 1 ---\nHello first page" in out and "--- page 2 ---\nSecond page here" in out


def test_a_pdf_with_no_text_layer_says_so_rather_than_returning_nothing(ws):
    _pdf(ws / "scan.pdf", ["", ""])
    out = _read(ws, "scan.pdf")
    assert out.startswith("Error:") and "no text layer" in out


def test_a_blank_page_among_text_pages_is_marked(ws):
    _pdf(ws / "p.pdf", ["Real text", ""])
    assert "--- page 2 ---\n(no text on this page)" in _read(ws, "p.pdf")


# ---------------- failures read as sentences ----------------
@pytest.mark.parametrize("name,payload,fragment", [
    ("fake.pdf", b"just text, not a pdf", "does not start like a PDF"),
    ("fake.docx", b"plain text", "not a real .docx"),
    ("fake.xlsx", b"\x00\x01\x02", "not a real .xlsx"),
])
def test_a_misnamed_file_gets_a_clear_message(ws, name, payload, fragment):
    (ws / name).write_bytes(payload)
    out = _read(ws, name)
    assert out.startswith("Error:") and fragment in out


def test_a_zip_that_is_not_the_right_office_file(ws):
    _zip(ws / "x.docx", {"readme.txt": "hi"})
    out = _read(ws, "x.docx")
    assert out.startswith("Error:") and "word/document.xml" in out


def test_a_damaged_zip(ws):
    p = _docx(ws / "d.docx")
    p.write_bytes(p.read_bytes()[:60])
    assert _read(ws, "d.docx").startswith("Error: could not read")


def test_xml_entities_are_refused(ws):
    xml = ('<?xml version="1.0"?><!DOCTYPE d [<!ENTITY a "aaaa">]>'
           f'<w:document {W}><w:body><w:p><w:r><w:t>&a;</w:t></w:r></w:p></w:body></w:document>')
    _zip(ws / "bomb.docx", {"word/document.xml": xml})
    out = _read(ws, "bomb.docx")
    assert out.startswith("Error:") and "entities" in out


def test_an_oversized_part_is_refused(ws, monkeypatch):
    monkeypatch.setattr(doc_readers, "MAX_MEMBER_BYTES", 100)
    _docx(ws / "r.docx")
    assert "too large to read" in _read(ws, "r.docx")


@pytest.mark.parametrize("name,label", [("old.doc", "Word 97-2003"), ("old.xls", "Excel 97-2003"), ("old.ppt", "PowerPoint 97-2003")])
def test_legacy_binary_formats_say_what_to_do(ws, name, label):
    (ws / name).write_bytes(b"\xd0\xcf\x11\xe0" + b"\x00" * 200)
    out = _read(ws, name)
    assert out.startswith("Error:") and label in out and "re-save" in out


# ---------------- unchanged behaviour ----------------
def test_csv_and_text_are_still_read_as_text(ws):
    (ws / "t.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    assert _read(ws, "t.csv") == "a,b\n1,2\n"


def test_a_real_binary_is_still_refused(ws):
    (ws / "m.gguf").write_bytes(bytes(range(256)) * 40)
    assert "not text" in _read(ws, "m.gguf")


def test_found_by_name_elsewhere_in_the_workspace_still_says_so(ws):
    (ws / "files").mkdir()
    _docx(ws / "files" / "report.docx")
    out = _read(ws, "report.docx")
    assert out.startswith("[Nothing at 'report.docx'; this is 'files/report.docx']\n[Word document]")


def test_the_second_read_comes_from_the_cache(ws, monkeypatch):
    _docx(ws / "report.docx")
    _read(ws, "report.docx")
    monkeypatch.setattr(doc_readers, "_read_docx", lambda p: pytest.fail("parsed twice"))
    assert _read(ws, "report.docx").startswith("[Word document]")


def test_an_edited_file_is_not_served_from_the_cache(ws):
    p = _docx(ws / "report.docx")
    first = _read(ws, "report.docx")
    _zip(p, {"word/document.xml": f'<w:document {W}><w:body><w:p><w:r><w:t>Changed</w:t></w:r></w:p></w:body></w:document>'})
    second = _read(ws, "report.docx")
    assert first != second and "Changed" in second


# ---------------- Excel dates: shown as dates only where the number format says so ----------------
def _xlsx_with_styles(path: Path, numfmts: str, xfs: list, cells: str, workbook_pr: str = "") -> Path:
    """xfs = numFmtId per cell style index (0, 1, 2...). cells = <c> elements of one row."""
    styles = (f'<styleSheet {S}><numFmts>{numfmts}</numFmts><cellXfs>'
              + "".join(f'<xf numFmtId="{i}"/>' for i in xfs) + "</cellXfs></styleSheet>")
    return _zip(path, {
        "xl/workbook.xml": f'<workbook {S} {R}>{workbook_pr}<sheets><sheet name="S" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": _rels(("rId1", "worksheets/sheet1.xml")),
        "xl/styles.xml": styles,
        "xl/worksheets/sheet1.xml": f'<worksheet {S}><sheetData><row r="1">{cells}</row></sheetData></worksheet>',
    })


def test_excel_dates_times_and_plain_numbers(ws):
    numfmts = ('<numFmt numFmtId="164" formatCode="yyyy-mm-dd hh:mm"/>'
               '<numFmt numFmtId="165" formatCode="0.00&quot; months&quot;"/>'   # d and m only inside quotes: a number
               '<numFmt numFmtId="166" formatCode="[Red]#,##0.0"/>'
               '<numFmt numFmtId="167" formatCode="h:mm:ss"/>')
    # style 0 General, 1 builtin date (14), 2 custom datetime, 3 "months" number, 4 [Red] number, 5 time, 6 builtin 2 (0.00)
    cells = ('<c r="A1" s="1"><v>46298</v></c>'          # 2026-10-03
             '<c r="B1" s="2"><v>46298.5</v></c>'        # + noon
             '<c r="C1" s="3"><v>46298</v></c>'          # not a date
             '<c r="D1" s="4"><v>46298</v></c>'          # not a date
             '<c r="E1" s="5"><v>0.75</v></c>'           # 18:00:00 -> time only
             '<c r="F1" s="6"><v>46298</v></c>'          # builtin 2 is not a date format
             '<c r="G1"><v>46298</v></c>'                # General
             '<c r="H1" s="1" t="s"><v>0</v></c>')       # a date style on a text cell stays text
    p = _xlsx_with_styles(ws / "d.xlsx", numfmts, [0, 14, 164, 165, 166, 167, 2], cells)
    with zipfile.ZipFile(p, "a") as zf:
        zf.writestr("xl/sharedStrings.xml", f'<sst {S}><si><t>n/a</t></si></sst>')
    out = _read(ws, "d.xlsx")
    assert "1: 2026-10-03\t2026-10-03 12:00\t46298\t46298\t18:00\t46298\t46298\tn/a" in out


def test_excel_1900_quirk_and_the_1904_system(ws):
    # serial 1 = 1900-01-01, 59 = 1900-02-28; 61 = 1900-03-01 (Excel has a phantom 1900-02-29 at 60)
    cells = '<c r="A1" s="1"><v>1</v></c><c r="B1" s="1"><v>59</v></c><c r="C1" s="1"><v>61</v></c><c r="D1" s="1"><v>0</v></c>'
    _xlsx_with_styles(ws / "e.xlsx", "", [0, 14], cells)
    assert "1: 1900-01-01\t1900-02-28\t1900-03-01\t0" in _read(ws, "e.xlsx")  # day 0 has no date: left as the number
    _xlsx_with_styles(ws / "m.xlsx", "", [0, 14], '<c r="A1" s="1"><v>0</v></c>', workbook_pr='<workbookPr date1904="1"/>')
    assert "1: 1904-01-01" in _read(ws, "m.xlsx")


def test_a_workbook_without_styles_keeps_raw_numbers(ws):
    _zip(ws / "n.xlsx", {
        "xl/workbook.xml": f'<workbook {S} {R}><sheets><sheet name="S" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": _rels(("rId1", "worksheets/sheet1.xml")),
        "xl/worksheets/sheet1.xml": f'<worksheet {S}><sheetData><row r="1"><c r="A1" s="1"><v>46298</v></c></row></sheetData></worksheet>',
    })
    assert "1: 46298" in _read(ws, "n.xlsx")


# ---------------- OpenDocument, EPUB, RTF ----------------
OT = 'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" ' \
     'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" ' \
     'xmlns:presentation="urn:oasis:names:tc:opendocument:xmlns:presentation:1.0"'


def _odf(path: Path, body: str) -> Path:
    return _zip(path, {"mimetype": "application/vnd.oasis.opendocument.text",
                       "content.xml": f'<office:document-content {OT}><office:body>{body}</office:body></office:document-content>'})


def test_odt_headings_lists_tables_spaces_and_footnotes_left_out(ws):
    _odf(ws / "a.odt", (
        '<office:text><text:h text:outline-level="2">Goals</text:h>'
        '<text:p>Two<text:s text:c="3"/>spaces<text:tab/>and a tab<text:line-break/>break'
        '<text:note><text:note-body><text:p>FOOTNOTE</text:p></text:note-body></text:note></text:p>'
        '<text:list><text:list-item><text:p>one</text:p><text:list><text:list-item><text:p>nested</text:p></text:list-item></text:list>'
        '</text:list-item><text:list-item><text:p>two</text:p></text:list-item></text:list>'
        '<table:table><table:table-row><table:table-cell><text:p>H1</text:p></table:table-cell>'
        '<table:table-cell table:number-columns-repeated="2"><text:p>x</text:p></table:table-cell></table:table-row></table:table>'
        '</office:text>'))
    out = _read(ws, "a.odt")
    assert out.startswith("[OpenDocument text]\n## Goals\n")
    assert "Two   spaces\tand a tab\nbreak" in out and "FOOTNOTE" not in out
    assert "- one" in out and "- nested" in out and "- two" in out
    assert "| H1 | x | x |" in out


def test_ods_values_dates_booleans_formulas_gaps_and_repeats(ws):
    _odf(ws / "a.ods", (
        '<office:spreadsheet><table:table table:name="Budget">'
        '<table:table-row><table:table-cell office:value-type="string"><text:p>Item</text:p></table:table-cell>'
        '<table:table-cell office:value-type="float" office:value="3.5"><text:p>3.50</text:p></table:table-cell>'
        '<table:table-cell office:value-type="date" office:date-value="2026-10-03"><text:p>10/03/26</text:p></table:table-cell>'
        '<table:table-cell office:value-type="date" office:date-value="2026-10-03T09:30:00"><text:p>x</text:p></table:table-cell>'
        '<table:table-cell office:value-type="boolean" office:boolean-value="true"><text:p>TRUE</text:p></table:table-cell></table:table-row>'
        '<table:table-row table:number-rows-repeated="2"><table:table-cell table:number-columns-repeated="9"/></table:table-row>'
        '<table:table-row><table:table-cell/><table:table-cell office:value-type="float" office:value="7" '
        'table:formula="of:=SUM([.B1:.B1];[$Other.A1])"><text:p>7</text:p></table:table-cell></table:table-row>'
        '<table:table-row table:number-rows-repeated="1048000"><table:table-cell table:number-columns-repeated="1024"/></table:table-row>'
        '</table:table><table:table table:name="Empty"/></office:spreadsheet>'))
    out = _read(ws, "a.ods")
    assert out.startswith("[OpenDocument spreadsheet, 2 sheets]")
    assert "1: Item\t3.5\t2026-10-03\t2026-10-03 09:30\tTRUE" in out
    assert "\n4: \t=SUM(B1:B1,Other!A1) -> 7" in out                       # rows 2-3 were an empty repeat: the gap holds
    assert "--- sheet: Empty ---\n(empty)" in out


def _epub(path: Path, chapters: dict, order: list) -> Path:
    items = "".join(f'<item id="{k}" href="{k}.xhtml" media-type="application/xhtml+xml"/>' for k in chapters)
    spine = "".join(f'<itemref idref="{k}"/>' for k in order)
    opf = ('<package xmlns="http://www.idpf.org/2007/opf" version="3.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
           f'<dc:title>Notes on Graphs</dc:title><dc:creator>A. Student</dc:creator></metadata><manifest>{items}</manifest><spine>{spine}</spine></package>')
    parts = {"mimetype": "application/epub+zip",
             "META-INF/container.xml": ('<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0"><rootfiles>'
                                        '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>'),
             "OEBPS/content.opf": opf}
    for k, html in chapters.items():
        parts[f"OEBPS/{k}.xhtml"] = html
    return _zip(path, parts)


def test_epub_follows_the_spine_order_and_keeps_headings_lists_and_table_rows(ws):
    ch1 = ('<html><head><title>IGNORED</title><style>p{}</style></head><body><h1>Intro</h1><p>First &amp; <b>bold</b>.</p>'
           '<ul><li>a</li><li>b</li></ul><table><tr><td><p>H1</p></td><td>H2</td></tr><tr><td>1</td><td>2</td></tr></table>'
           '<script>alert(1)</script></body></html>')
    ch2 = "<html><body><h2>Second</h2><p>Later.</p></body></html>"
    _epub(ws / "b.epub", {"c1": ch1, "c2": ch2}, order=["c2", "c1"])          # the spine says c2 first
    out = _read(ws, "b.epub")
    assert out.startswith("[EPUB: Notes on Graphs by A. Student, 2 sections]")
    assert out.index("## Second") < out.index("# Intro")
    assert "First & bold." in out and "- a" in out and "- b" in out
    assert "| H1 | H2 |" in out and "| 1 | 2 |" in out
    assert "IGNORED" not in out and "alert" not in out


def test_rtf_text_escapes_unicode_and_skipped_destinations(ws):
    rtf = (r"{\rtf1\ansi\ansicpg1252\deff0{\fonttbl{\f0 Arial;}}{\colortbl;\red0\green0\blue0;}"
           r"{\*\generator Hidden Words;}{\info{\title SECRET}}"
           r"\pard Caf\'e9 \u8364?  and \u233? \b bold\b0\par Line two with a \{brace\} and a tab\tab here\par "
           r"{\pict skip me}End\~nbsp\par}")
    (ws / "a.rtf").write_text(rtf, encoding="latin-1")
    out = _read(ws, "a.rtf")
    assert out.startswith("[RTF document]\nCafé €  and é bold")  # the "?" after \\u8364 is its fallback and is dropped; both real spaces stay
    assert "Line two with a {brace} and a tab\there" in out
    assert "Hidden Words" not in out and "SECRET" not in out and "skip me" not in out and "Arial" not in out
    assert "End nbsp" in out


@pytest.mark.parametrize("name,payload,fragment", [
    ("fake.rtf", b"just text", "does not start like an RTF"),
    ("fake.odt", b"plain", "not a real .odt"),
    ("fake.epub", b"plain", "not a real .epub"),
])
def test_misnamed_open_formats_get_a_clear_message(ws, name, payload, fragment):
    (ws / name).write_bytes(payload)
    assert fragment in _read(ws, name) and _read(ws, name).startswith("Error:")


def test_an_odf_drawing_is_declined_politely(ws):
    _odf(ws / "d.odt", "<office:drawing/>")
    assert "a kind Jarvis does not read" in _read(ws, "d.odt")


# ---------------- review fixes (2026-11-05) ----------------
def test_a_drm_protected_epub_is_refused_but_font_obfuscation_is_fine(ws):
    enc = lambda algo, uri: ('<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" '
                             'xmlns:enc="http://www.w3.org/2001/04/xmlenc#"><enc:EncryptedData>'
                             f'<enc:EncryptionMethod Algorithm="{algo}"/><enc:CipherData><enc:CipherReference URI="{uri}"/>'
                             '</enc:CipherData></enc:EncryptedData></encryption>')
    book = _epub(ws / "locked.epub", {"c1": "<html><body><p>Readable words.</p></body></html>"}, ["c1"])
    with zipfile.ZipFile(book, "a") as zf:
        zf.writestr("META-INF/encryption.xml", enc("http://www.w3.org/2001/04/xmlenc#aes128-cbc", "OEBPS/c1.xhtml"))
    out = _read(ws, "locked.epub")
    assert out.startswith("Error:") and "DRM-protected" in out
    fonts = _epub(ws / "fonts.epub", {"c1": "<html><body><p>Readable words.</p></body></html>"}, ["c1"])
    with zipfile.ZipFile(fonts, "a") as zf:
        zf.writestr("META-INF/encryption.xml", enc("http://www.idpf.org/2008/embedding", "OEBPS/fonts/a.otf"))
    assert "Readable words." in _read(ws, "fonts.epub")


def test_rtf_emoji_and_mac_codepage(ws):
    (ws / "e.rtf").write_text(r"{\rtf1\ansi\ansicpg10000 caf\'8e \uc1\u-10179?\u-8704? \u-10179?only\par}", encoding="latin-1")
    out = _read(ws, "e.rtf")
    assert "café 😀" in out and "�only" in out      # Mac Roman 0x8E is é; a lone surrogate becomes U+FFFD
    out.encode("utf-8")                                   # nothing left that can't be encoded
    (ws / "u.rtf").write_text(r"{\rtf1\ansi\ansicpg99999 plain\par}", encoding="latin-1")
    assert "plain" in _read(ws, "u.rtf")                 # an unknown code page falls back instead of failing


def test_columns_past_the_limit_are_announced_so_a_write_back_is_refused(ws):
    from app.agent.tools import doc_readers
    from app.agent.tools.doc_writers import BuildError, build_document

    data, _, _ = build_document(".xlsx", ",".join(f"c{i}" for i in range(doc_readers.MAX_SHEET_COLS + 5)))
    (ws / "wide.xlsx").write_bytes(data)
    text = _read(ws, "wide.xlsx")
    assert "cells past column CV not read" in text and doc_readers.CUT_RE.search(text)
    with pytest.raises(BuildError, match="wider than read_file shows"):
        build_document(".xlsx", text)


# ---------------- Word pictures as keep: markers ----------------
_NS = ('xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
       'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
       'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
       'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
       'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" xmlns:v="urn:schemas-microsoft-com:vml"')


def _drawing(rid: str, descr: str = "", anchor: bool = False, uri: str = "http://schemas.openxmlformats.org/drawingml/2006/picture") -> str:
    tag = "wp:anchor" if anchor else "wp:inline"
    blip = f'<a:blip r:embed="{rid}"/>' if rid else ""
    return (f'<w:drawing><{tag}><wp:extent cx="914400" cy="457200"/><wp:docPr id="1" name="P" descr="{descr}"/>'
            f'<a:graphic><a:graphicData uri="{uri}">{blip}</a:graphicData></a:graphic></{tag}></w:drawing>')


def test_word_pictures_read_as_keep_markers_in_place(tmp_path):
    body = (
        '<w:p><w:r><w:t>Intro text</w:t></w:r></w:p>'
        f'<w:p><w:r>{_drawing("rId5", "Sales [2026] (draft)")}</w:r></w:p>'                    # a picture alone
        f'<w:p><w:r>{_drawing("rId6", anchor=True)}</w:r><w:r><w:t>Floating, before text</w:t></w:r></w:p>'
        '<w:p><w:r><mc:AlternateContent><mc:Choice Requires="wps">' + _drawing("rId5") + '</mc:Choice>'
        '<mc:Fallback><w:pict><v:shape><v:imagedata r:id="rId5"/></v:shape></w:pict></mc:Fallback></mc:AlternateContent></w:r></w:p>'
        f'<w:p><w:r>{_drawing("", uri="http://schemas.openxmlformats.org/drawingml/2006/chart")}</w:r></w:p>'
        '<w:p><w:r><w:pict><v:shape><v:imagedata r:id="rId7"/></v:shape></w:pict></w:r></w:p>'   # an older Word file
        '<w:p><w:r><w:t>Outro</w:t></w:r></w:p>')
    rels = ('<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId5" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image1.png"/>'
            '<Relationship Id="rId6" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/photo.jpeg"/>'
            '<Relationship Id="rId7" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="/word/media/old.wmf"/>'
            '<Relationship Id="rId8" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="http://x/y.png" TargetMode="External"/>'
            '</Relationships>')
    path = _zip(tmp_path / "pics.docx", {"word/document.xml": f'<?xml version="1.0"?><w:document {_NS}><w:body>{body}</w:body></w:document>',
                                          "word/_rels/document.xml.rels": rels})
    text = doc_readers.read_document(path, "pics.docx")
    assert text.splitlines() == [
        "[Word document]", doc_readers.KEEP_NOTE, "Intro text",
        "![Sales 2026 draft](keep:image1.png)",          # brackets in the alt text would end the marker early
        "![](keep:photo.jpeg)", "Floating, before text",  # the picture came first in its paragraph
        "![](keep:image1.png)",                           # Choice and Fallback are one picture
        "[chart: not kept if this document is rewritten]",
        "![](keep:old.wmf)", "Outro"]
    from app.agent.tools.doc_writers import prepare_text
    back, readback = prepare_text(text)
    assert readback and doc_readers.KEEP_NOTE not in back and "[chart:" not in back and "![](keep:old.wmf)" in back


def test_a_word_file_without_pictures_reads_as_before(tmp_path):
    text = doc_readers.read_document(_docx(tmp_path / "plain.docx"), "plain.docx")
    assert "keep:" not in text and doc_readers.KEEP_NOTE not in text
