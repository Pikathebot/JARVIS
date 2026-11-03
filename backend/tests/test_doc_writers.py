"""write_file builds Word, Excel and PDF files from text (PLAN 4.27, writing).

Everything is checked through the tools the model uses: write_file to make the file, read_file to read it back.
The files were also opened by python-docx, openpyxl and LibreOffice by hand when this was written (see PLAN 4.27);
those libraries are not requirements, so they are not used here.
"""
import re
import zipfile
from pathlib import Path

import pytest

from app.agent.tools import doc_writers
from app.agent.tools.doc_writers import BuildError, build_document
from app.agent.tools.file_safety import backup_dir
from app.agent.tools.read_file import read_file
from app.agent.tools.write_file import write_file


@pytest.fixture(autouse=True)
def _fresh_reader_cache():
    from app.agent.tools import doc_readers
    doc_readers._CACHE.clear()
    yield
    doc_readers._CACHE.clear()


def _write(ws: Path, name: str, content: str, **kw) -> str:
    return write_file(name, content, workspace_path=str(ws), **kw)


def _read(ws: Path, name: str, **kw) -> str:
    return read_file(name, workspace_path=str(ws), **kw)


def _parts(path: Path) -> dict:
    with zipfile.ZipFile(path) as zf:
        return {n: zf.read(n).decode("utf-8") for n in zf.namelist()}


# ---------------- Word ----------------
DOC = """# Study plan

Prepared for **Autumn** with *care* & a < b > c.

## Goals
- Finish trees
- Solve 20 graph problems

1. Start easy
2. Time every attempt

Some text between.

1. A new list restarts at one

| Week | Topic |
|------|-------|
| 1 | Arrays |
| 2 | Trees |
"""


def test_docx_round_trips_through_write_file_and_read_file(tmp_path):
    out = _write(tmp_path, "plan.docx", DOC)
    assert out.startswith("Successfully wrote a Word document (") and "1 table" in out and "'plan.docx'" in out
    text = _read(tmp_path, "plan.docx").splitlines()
    assert text[0] == "[Word document]"
    assert "# Study plan" in text and "## Goals" in text
    assert "Prepared for Autumn with care & a < b > c." in text           # entities survived, markers became formatting
    assert "- Finish trees" in text and "- Solve 20 graph problems" in text
    assert "- Start easy" in text and "- A new list restarts at one" in text
    assert "| Week | Topic |" in text and "| 2 | Trees |" in text


def test_docx_has_real_formatting_numbering_restart_and_a_table_grid(tmp_path):
    _write(tmp_path, "plan.docx", DOC)
    parts = _parts(tmp_path / "plan.docx")
    doc = parts["word/document.xml"]
    assert '<w:pStyle w:val="Heading1"/>' in doc and '<w:pStyle w:val="Heading2"/>' in doc
    assert "<w:b/>" in doc and "<w:i/>" in doc                            # **bold** and *italic* became run properties
    assert "<w:tblGrid>" in doc and '<w:tblStyle w:val="TableGrid"/>' in doc and "<w:tblHeader/>" in doc
    assert doc.rstrip().endswith("</w:body></w:document>")
    # bullets share list 1; each numbered list is its own w:num with a start override, so the second one starts at 1
    nums = re.findall(r'<w:num w:numId="(\d+)">', parts["word/numbering.xml"])
    assert nums[0] == "1" and len(nums) == 3 and parts["word/numbering.xml"].count("startOverride") == 2
    assert "[Content_Types].xml" in parts and "_rels/.rels" in parts and "word/styles.xml" in parts


def test_docx_text_is_xml_safe(tmp_path):
    _write(tmp_path, "x.docx", "A \x00bad\x0b control & <tag> char\n")
    parts = _parts(tmp_path / "x.docx")
    assert "&amp;" in parts["word/document.xml"] and "&lt;tag&gt;" in parts["word/document.xml"]
    assert "\x00" not in parts["word/document.xml"] and "\x0b" not in parts["word/document.xml"]
    assert "A bad control & <tag> char" in _read(tmp_path, "x.docx")


def test_docx_with_only_a_table_still_ends_in_a_paragraph(tmp_path):
    _write(tmp_path, "t.docx", "| a | b |\n| 1 | 2 |\n")
    assert _parts(tmp_path / "t.docx")["word/document.xml"].count("<w:p/><w:sectPr>") == 1  # Word rejects a table last in the body


# ---------------- Excel ----------------
SHEET = """Item,Qty,Price,Bought,Code,Done
Pens,3,1.5,2026-10-03,007,TRUE
"Notebook, A5",2,3.25,2026-10-04 09:30,12,false
Total,=SUM(B2:B3),=B2*C2+B3*C3,,,
--- sheet: Notes & more ---
tab\tseparated\t42
"""


def test_xlsx_round_trips_with_dates_formulas_and_two_sheets(tmp_path):
    out = _write(tmp_path, "b.xlsx", SHEET)
    assert out.startswith("Successfully wrote an Excel workbook (2 sheets, 5 rows, 2 formulas that Excel calculates")
    text = _read(tmp_path, "b.xlsx")
    assert "--- sheet: Sheet1 ---" in text and "--- sheet: Notes & more ---" in text
    assert "1: Item\tQty\tPrice\tBought\tCode\tDone" in text
    assert "2: Pens\t3\t1.5\t2026-10-03\t007\tTRUE" in text                # date style read back as a date; 007 stayed text
    assert "3: Notebook, A5\t2\t3.25\t2026-10-04 09:30\t12\tFALSE" in text   # quoted comma; datetime; false -> FALSE
    assert "4: Total\t=SUM(B2:B3)\t=B2*C2+B3*C3" in text
    assert "1: tab\tseparated\t42" in text                                  # a tab-separated line, on the second sheet


def test_xlsx_formulas_recalculate_on_open_and_numbers_are_numbers(tmp_path):
    _write(tmp_path, "b.xlsx", SHEET)
    parts = _parts(tmp_path / "b.xlsx")
    assert 'fullCalcOnLoad="1"' in parts["xl/workbook.xml"]
    sheet1 = parts["xl/worksheets/sheet1.xml"]
    assert "<f>SUM(B2:B3)</f>" in sheet1 and "<f>B2*C2+B3*C3</f>" in sheet1
    assert '<c r="B2"><v>3</v></c>' in sheet1 and '<c r="C2"><v>1.5</v></c>' in sheet1
    assert '<c r="D2" s="1"><v>46298</v></c>' in sheet1                     # 2026-10-03 as an Excel serial with a date style
    assert 't="inlineStr"' in sheet1 and "Notebook, A5" in sheet1 and ">007<" in sheet1
    assert '<c r="F2" t="b"><v>1</v></c>' in sheet1


def test_xlsx_sheet_names_are_made_valid_and_unique(tmp_path):
    _write(tmp_path, "n.xlsx", "--- sheet: a/b:c ---\n1\n--- sheet: A/B:C ---\n2\n--- sheet: " + "x" * 50 + " ---\n3\n")
    wb = _parts(tmp_path / "n.xlsx")["xl/workbook.xml"]
    names = re.findall(r'<sheet name="([^"]*)"', wb)
    assert names[0] == "a b c" and names[1] == "A B C (2)" and len(names[2]) == 31


def test_xlsx_caps_rows_and_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(doc_writers, "MAX_XLSX_ROWS", 3)
    out = _write(tmp_path, "c.xlsx", "\n".join(str(i) for i in range(1, 9)))
    assert "cut at 3 rows" in out
    assert "4:" not in _read(tmp_path, "c.xlsx").replace("(row number first", "")


# ---------------- PDF ----------------
PDF_TEXT = """# Lab report

Measured **voltage** and *current* for the resistor.

- first point
- second point

| Trial | Volts |
|---|---|
| 1 | 2.0 |
| 2 | 4.0 |
"""


def test_pdf_round_trips_through_read_file(tmp_path):
    out = _write(tmp_path, "r.pdf", PDF_TEXT)
    assert out.startswith("Successfully wrote a PDF (1 page,") and "1 table" in out and "Warning" not in out
    text = _read(tmp_path, "r.pdf")
    assert text.startswith("[PDF, 1 page]") and "Lab report" in text
    assert "Measured voltage and current for the resistor." in text
    assert "first point" in text and "second point" in text and "Volts" in text and "4.0" in text


def test_pdf_runs_onto_more_pages(tmp_path):
    out = _write(tmp_path, "long.pdf", "\n\n".join(f"Paragraph {i} " + "word " * 80 for i in range(60)))
    assert re.search(r"a PDF \((\d+) pages", out) and int(re.search(r"\((\d+) pages", out).group(1)) > 1
    assert "Paragraph 59" in _read(tmp_path, "long.pdf")


def test_a_character_the_font_cannot_draw_is_replaced_and_reported(tmp_path):
    out = _write(tmp_path, "w.pdf", "Plain  text\n")   # a private-use character is in no font
    assert "Warning: 1 character(s)" in out and "?" in out
    assert "Plain ? text" in _read(tmp_path, "w.pdf")


def test_pdf_without_a_system_font_falls_back_to_helvetica(tmp_path, monkeypatch):
    monkeypatch.setattr(doc_writers, "_find_font_family", lambda: None)
    out = _write(tmp_path, "h.pdf", "Café — “quoted” ✓ done\n")  # cp1252 covers the first three, not the tick
    assert "Warning: 1 character(s)" in out
    text = _read(tmp_path, "h.pdf")
    assert "Café" in text and "done" in text


# ---------------- through write_file ----------------
def test_replacing_a_document_backs_up_the_old_one(tmp_path):
    _write(tmp_path, "plan.docx", "# First\n")
    out = _write(tmp_path, "plan.docx", "# Second\n")
    assert "The previous version is saved as '.jarvis/backups/plan.docx." in out
    assert "# Second" in _read(tmp_path, "plan.docx")
    assert len(list(backup_dir(tmp_path).glob("plan.docx.*.bak"))) == 1


def test_a_failed_build_leaves_the_existing_file_alone(tmp_path):
    _write(tmp_path, "keep.xlsx", "a,b\n1,2\n")
    before = (tmp_path / "keep.xlsx").read_bytes()
    out = _write(tmp_path, "keep.xlsx", "   \n")
    assert out.startswith("Error: could not build 'keep.xlsx': the content is empty")
    assert (tmp_path / "keep.xlsx").read_bytes() == before
    assert not list(backup_dir(tmp_path).glob("keep.xlsx.*")) if backup_dir(tmp_path).exists() else True


def test_overwrite_false_is_respected_for_documents(tmp_path):
    _write(tmp_path, "o.docx", "# one\n")
    assert "already exists and overwrite is set to False" in _write(tmp_path, "o.docx", "# two\n", overwrite=False)


def test_a_document_goes_into_a_new_subfolder(tmp_path):
    out = _write(tmp_path, "reports/2026/q3.pdf", "# Q3\n")
    assert out.startswith("Successfully wrote a PDF") and (tmp_path / "reports" / "2026" / "q3.pdf").is_file()


def test_the_extension_check_is_case_insensitive(tmp_path):
    assert _write(tmp_path, "UPPER.DOCX", "# Hi\n").startswith("Successfully wrote a Word document")


def test_text_files_and_csv_are_still_plain_text(tmp_path):
    assert _write(tmp_path, "t.csv", "a,b\n1,2\n").startswith("Successfully wrote")
    assert (tmp_path / "t.csv").read_text() == "a,b\n1,2\n"
    assert _write(tmp_path, "n.md", "# note\n").startswith("Successfully wrote 7 characters")


def test_no_temp_file_is_left_behind(tmp_path):
    _write(tmp_path, "x.docx", "# x\n")
    assert [p.name for p in tmp_path.iterdir() if p.is_file()] == ["x.docx"]


def test_build_document_rejects_unknown_extensions_and_huge_input():
    with pytest.raises(BuildError):
        build_document(".pptx", "x")
    with pytest.raises(BuildError, match="limit"):
        build_document(".docx", "x" * (doc_writers.MAX_CONTENT_CHARS + 1))


# ---------------- read -> change -> write: what read_file prints is accepted back ----------------
def _round_trip(ws: Path, name: str):
    """(first read, second read) after writing the first read back to the same file."""
    first = _read(ws, name)
    out = _write(ws, name, first)
    assert out.startswith("Successfully wrote"), out
    return first, _read(ws, name)


def test_word_round_trip_is_stable_and_keeps_paragraphs_separate(tmp_path):
    _write(tmp_path, "d.docx", DOC)
    first, second = _round_trip(tmp_path, "d.docx")
    assert first == second
    assert "Some text between." in second and "Prepared for Autumn" in second     # one paragraph per line, none merged
    assert second.count("[Word document]") == 1                                       # the header is not written in as text


def test_excel_round_trip_keeps_rows_formulas_hidden_flag_and_gaps(tmp_path):
    # a hidden sheet, an empty row 3, a formula that carries a saved value (what Excel writes), a quoted comma
    from tests.test_doc_readers import R, S, _rels
    parts = {
        "xl/workbook.xml": (f'<workbook {S} {R}><sheets><sheet name="Budget" sheetId="1" r:id="rId1"/>'
                            '<sheet name="Old" sheetId="2" state="hidden" r:id="rId2"/></sheets></workbook>'),
        "xl/_rels/workbook.xml.rels": _rels(("rId1", "worksheets/sheet1.xml"), ("rId2", "worksheets/sheet2.xml")),
        "xl/worksheets/sheet1.xml": (f'<worksheet {S}><sheetData>'
                                     '<row r="1"><c r="A1" t="inlineStr"><is><t>Item, big</t></is></c><c r="B1"><v>2</v></c></row>'
                                     '<row r="4"><c r="A4" t="inlineStr"><is><t>Sum</t></is></c><c r="B4"><f>SUM(B1:B1)</f><v>2</v></c></row>'
                                     '</sheetData></worksheet>'),
        "xl/worksheets/sheet2.xml": f'<worksheet {S}><sheetData/></worksheet>',
    }
    with zipfile.ZipFile(tmp_path / "x.xlsx", "w") as zf:
        for n, d in parts.items():
            zf.writestr(n, d)
    first = _read(tmp_path, "x.xlsx")
    assert "=SUM(B1:B1) -> 2" in first and "(hidden)" in first and "4: Sum" in first and "\n2:" not in first
    out = _write(tmp_path, "x.xlsx", first)
    assert "2 sheets, 2 rows, 1 formula" in out
    sheet1 = _parts(tmp_path / "x.xlsx")["xl/worksheets/sheet1.xml"]
    assert '<row r="4">' in sheet1 and '<c r="B4"><f>SUM(B1:B1)</f></c>' in sheet1   # row 4 stayed row 4; no "-> 2" in the formula
    assert "<row r=\"2\">" not in sheet1 and "Item, big" in sheet1                    # the gap stayed a gap; the comma stayed in the cell
    second = _read(tmp_path, "x.xlsx")
    assert second == first.replace("=SUM(B1:B1) -> 2", "=SUM(B1:B1)").replace("Old (hidden)", "Old")  # value gone (Excel recalcs), flag gone


def test_excel_round_trip_of_a_written_workbook_is_stable(tmp_path):
    _write(tmp_path, "b.xlsx", SHEET)
    first, second = _round_trip(tmp_path, "b.xlsx")
    assert first == second
    assert "Sheet1 (2)" not in second and "[Excel workbook" not in "\n".join(l for l in second.splitlines()[1:])


def test_pdf_round_trip_is_stable_and_drops_page_markers_from_the_body(tmp_path):
    _write(tmp_path, "r.pdf", PDF_TEXT)
    first, second = _round_trip(tmp_path, "r.pdf")
    assert first == second and second.count("--- page 1 ---") == 1 and second.startswith("[PDF, 1 page]")


def test_a_cut_document_is_refused_not_written_back_short(tmp_path):
    for marker in ("[... 7 more rows not read (limit 2000 per sheet) ...]", "[... 3 more pages not read (limit 1000) ...]",
                   "[... cut at 1,500,000 characters ...]", "[... 5,000 characters omitted from the middle of this read_file result ...]",
                   "[... 9 more chars not kept in history; run the tool again for them]"):
        out = _write(tmp_path, "cut.docx", f"# Title\n\nbody\n{marker}\n")
        assert out.startswith("Error: could not build 'cut.docx'") and "only part of the document" in out, marker
    assert not (tmp_path / "cut.docx").exists()


def test_a_line_range_from_read_file_is_refused_unless_it_is_the_whole_file(tmp_path):
    _write(tmp_path, "d.docx", DOC)
    part = _read(tmp_path, "d.docx", start_line=2, end_line=4)
    assert _write(tmp_path, "copy.docx", part).startswith("Error: could not build") and not (tmp_path / "copy.docx").exists()
    whole = _read(tmp_path, "d.docx", start_line=1, end_line=999)       # a range that covers everything is fine
    assert _write(tmp_path, "copy.docx", whole).startswith("Successfully wrote")
    assert "[Lines" not in _read(tmp_path, "copy.docx")


def test_excel_text_that_only_looks_like_a_row_number_is_not_one(tmp_path):
    # no read_file header: "3: apples" is a value in column A, not a row number
    _write(tmp_path, "w.xlsx", "3: apples,5\n12: pears,7\n")
    text = _read(tmp_path, "w.xlsx")
    assert "1: 3: apples\t5" in text and "2: 12: pears\t7" in text


# ---------------- tool-level behaviour ----------------
def test_patch_file_refuses_a_document_and_says_what_to_do_instead(tmp_path):
    from app.agent.tools.patch_file import patch_file
    _write(tmp_path, "p.docx", "# Title\n")
    out = patch_file("p.docx", "Title", "New", workspace_path=str(tmp_path))
    assert out.startswith("Error:") and "write_file the whole new text" in out
    (tmp_path / "s.pptx").write_bytes(b"PK\x03\x04")
    assert "can't change this kind of file" in patch_file("s.pptx", "a", "b", workspace_path=str(tmp_path))


def test_a_locked_document_says_to_close_it(tmp_path, monkeypatch):
    _write(tmp_path, "locked.docx", "# one\n")
    monkeypatch.setattr(doc_writers.os, "replace", lambda *a, **k: (_ for _ in ()).throw(PermissionError("[WinError 32]")))
    out = _write(tmp_path, "locked.docx", "# two\n")
    assert out.startswith("Error:") and "open in another program" in out and "WinError" not in out
    assert not list(tmp_path.glob("*.tmp"))                                  # the temp file was cleaned up


def test_the_pdf_font_choice_prefers_a_family_with_an_italic_face(tmp_path, monkeypatch):
    fonts = tmp_path / "fonts"
    fonts.mkdir()
    for name in ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf",                    # no italic: would render upright
                 "LiberationSans-Regular.ttf", "LiberationSans-Bold.ttf", "LiberationSans-Italic.ttf", "LiberationSans-BoldItalic.ttf"):
        (fonts / name).write_bytes(b"x")
    monkeypatch.setattr(doc_writers, "_font_dirs", lambda: [fonts])
    family = doc_writers._find_font_family()
    assert [p.name for p in family] == ["LiberationSans-Regular.ttf", "LiberationSans-Bold.ttf",
                                        "LiberationSans-Italic.ttf", "LiberationSans-BoldItalic.ttf"]
    (fonts / "LiberationSans-Italic.ttf").unlink()
    assert doc_writers._find_font_family()[0].name == "DejaVuSans.ttf"        # none complete: first regular found


# ---------------- write_file never writes plain text into a format it cannot build ----------------
@pytest.mark.parametrize("name", ["deck.pptx", "notes.odt", "sheet.ods", "book.epub", "memo.rtf", "macro.xlsm", "old.doc", "old.xls", "old.ppt"])
def test_a_new_file_of_a_readable_but_unbuildable_format_is_refused(tmp_path, name):
    out = _write(tmp_path, name, "some text\n")
    assert out.startswith(f"Error: Jarvis can't write {Path(name).suffix} files") and "Write a .docx, .xlsx or .pdf" in out
    assert not (tmp_path / name).exists()


def test_an_existing_unbuildable_file_is_left_alone_and_not_backed_up(tmp_path):
    (tmp_path / "notes.odt").write_bytes(b"PK\x03\x04 original bytes")
    out = _write(tmp_path, "notes.odt", "# edited\n")
    assert out.startswith("Error: Jarvis can't write .odt files")
    assert (tmp_path / "notes.odt").read_bytes() == b"PK\x03\x04 original bytes"
    assert not backup_dir(tmp_path).exists() or not list(backup_dir(tmp_path).glob("notes.odt.*"))


def test_plain_text_formats_are_still_written(tmp_path):
    for name in ("a.txt", "b.md", "c.csv", "d.json", "e.py"):
        assert _write(tmp_path, name, "x\n").startswith("Successfully wrote")


# ---------------- converting between formats: what the other readers print is not content ----------------
def _plain(text: str) -> list:
    return [l for l in text.splitlines()]


def test_ods_converts_to_xlsx_without_a_phantom_sheet_or_junk_rows(tmp_path):
    from tests.test_doc_readers import OT, _odf

    _odf(tmp_path / "a.ods", (
        '<office:spreadsheet><table:table table:name="By category"><table:table-row><table:table-cell office:value-type="string"><text:p>Item</text:p></table:table-cell>'
        '<table:table-cell office:value-type="float" office:value="7" table:formula="of:=SUM([.B1:.B1])+[$\'By category\'.A9]"><text:p>7</text:p></table:table-cell>'
        '</table:table-row></table:table><table:table table:name="Two"><table:table-row><table:table-cell office:value-type="float" office:value="1">'
        '<text:p>1</text:p></table:table-cell></table:table-row></table:table></office:spreadsheet>'))
    first = _read(tmp_path, "a.ods")
    assert first.startswith("[OpenDocument spreadsheet, 2 sheets]")
    assert "=SUM(B1:B1)+'By category'!A9 -> 7" in first                       # a quoted sheet name keeps its quotes
    assert _write(tmp_path, "b.xlsx", first).startswith("Successfully wrote an Excel workbook (2 sheets, 2 rows, 1 formula")
    wb = _parts(tmp_path / "b.xlsx")["xl/workbook.xml"]
    assert re.findall(r'<sheet name="([^"]*)"', wb) == ["By category", "Two"]
    assert "<f>SUM(B1:B1)+'By category'!A9</f>" in _parts(tmp_path / "b.xlsx")["xl/worksheets/sheet1.xml"]
    assert "OpenDocument" not in " ".join(_parts(tmp_path / "b.xlsx").values()).replace("urn:oasis:names:tc:opendocument", "")


@pytest.mark.parametrize("source", ["odt", "rtf", "epub", "pptx", "odp"])
def test_other_formats_convert_to_docx_and_pdf_with_no_headers_or_markers_in_the_body(tmp_path, source):
    from tests.test_doc_readers import OT, _epub, _odf, _pptx, _zip

    if source == "odt":
        _odf(tmp_path / "s.odt", '<office:text><text:h text:outline-level="1">Heading</text:h><text:p>Body line.</text:p></office:text>')
    elif source == "rtf":
        (tmp_path / "s.rtf").write_text(r"{\rtf1\ansi Heading\par Body line.\par}", encoding="latin-1")
    elif source == "epub":
        _epub(tmp_path / "s.epub", {"c1": "<html><body><h1>Heading</h1><p>Body line.</p></body></html>"}, ["c1"])
    elif source == "pptx":
        _pptx(tmp_path / "s.pptx")
    else:
        _odf(tmp_path / "s.odp", ('<office:presentation><draw:page draw:name="p1"><draw:frame><draw:text-box><text:p>Heading</text:p>'
                                  '<text:p>Body line.</text:p></draw:text-box></draw:frame></draw:page></office:presentation>'))
    first = _read(tmp_path, f"s.{source}")
    for target in ("docx", "pdf"):
        assert _write(tmp_path, f"out.{target}", first).startswith("Successfully wrote")
        text = _read(tmp_path, f"out.{target}")
        for junk in ("[OpenDocument", "[EPUB", "[RTF document]", "[PowerPoint", "--- slide", "--- section", "(no text"):
            assert junk not in text, (source, target, junk)
        assert "Heading" in text or "Welcome" in text
    docx = _parts(tmp_path / "out.docx")["word/document.xml"]
    assert docx.count("<w:p>") >= 2 and "[" not in re.sub(r"<[^>]+>", "", docx)[:1]


def test_the_reader_and_writer_share_one_set_of_markers():
    from app.agent.tools import doc_readers

    assert doc_writers._HEADER is doc_readers.HEADER_RE and doc_writers._READBACK_LINE is doc_readers.SCAFFOLD_RE
    for header in ("[Word document]", "[PDF, 3 pages]", "[Excel workbook, 2 sheets]", "[PowerPoint, 4 slides]", "[OpenDocument text]",
                   "[OpenDocument spreadsheet, 2 sheets]", "[OpenDocument presentation, 5 slides]", "[EPUB: A Book by Me, 12 sections]",
                   "[EPUB, 1 section]", "[RTF document]"):
        assert doc_readers.HEADER_RE.match(header), header
    for line in ("--- page 2 ---", "--- section 3 ---", "--- slide 1 ---", "(empty)", "(no text on this slide)", "(too large to read)"):
        assert doc_readers.SCAFFOLD_RE.match(line), line
    assert not doc_readers.HEADER_RE.match("[Draft] my own bracketed line")
