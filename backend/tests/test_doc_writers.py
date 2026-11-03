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
