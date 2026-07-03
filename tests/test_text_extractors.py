import tempfile
import unittest
import zipfile
from email.message import EmailMessage
from pathlib import Path

import _pathfix  # noqa: F401

from src.broker import text_extractors


class TestTextExtractors(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, name: str, data: bytes) -> Path:
        path = self.tmp_path / name
        path.write_bytes(data)
        return path

    def test_txt_extraction(self):
        path = self._write("note.txt", b"Base Income: $85,000\nEmployer: Acme Pty Ltd")
        result = text_extractors.extract_text(path)
        self.assertIn("Base Income", result.text)
        self.assertEqual(result.warnings, [])

    def test_csv_extraction(self):
        path = self._write("data.csv", b"name,amount\nJohn,85000\n")
        result = text_extractors.extract_text(path)
        self.assertIn("John, 85000", result.text)

    def test_tsv_extraction(self):
        path = self._write("data.tsv", b"name\tamount\nJohn\t85000\n")
        result = text_extractors.extract_text(path)
        self.assertIn("John, 85000", result.text)

    def test_html_extraction_strips_tags(self):
        html = b"<html><body><h1>Statement</h1><p>Balance: $1,234</p></body></html>"
        path = self._write("page.html", html)
        result = text_extractors.extract_text(path)
        self.assertNotIn("<p>", result.text)
        self.assertIn("Balance: $1,234", result.text)

    def test_json_extraction(self):
        path = self._write("data.json", b'{"amount": 85000, "name": "John"}')
        result = text_extractors.extract_text(path)
        self.assertIn("85000", result.text)
        self.assertIn("John", result.text)

    def test_eml_extraction_plain_text(self):
        msg = EmailMessage()
        msg["From"] = "client@example.com"
        msg["Date"] = "Mon, 1 Jul 2024 10:00:00 +1000"
        msg["Subject"] = "Loan documents"
        msg.set_content("Hi, please find my payslip attached.\nRegards,\nJohn")
        path = self._write("client.eml", bytes(msg))
        result = text_extractors.extract_text(path)
        self.assertIn("From: client@example.com", result.text)
        self.assertIn("Subject: Loan documents", result.text)
        self.assertIn("please find my payslip", result.text)

    def test_eml_extraction_html_fallback(self):
        msg = EmailMessage()
        msg["From"] = "client@example.com"
        msg["Date"] = "Mon, 1 Jul 2024 10:00:00 +1000"
        msg["Subject"] = "Loan documents"
        msg.set_content("<html><body><p>Please see attached <b>payslip</b>.</p></body></html>", subtype="html")
        path = self._write("client_html.eml", bytes(msg))
        result = text_extractors.extract_text(path)
        self.assertIn("Please see attached", result.text)
        self.assertNotIn("<b>", result.text)

    def test_docx_extraction(self):
        document_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="ns"><w:body>'
            '<w:p><w:r><w:t>Base Income: $85,000</w:t></w:r></w:p>'
            '<w:p><w:r><w:t>Employer: Acme Pty Ltd</w:t></w:r></w:p>'
            '</w:body></w:document>'
        )
        path = self.tmp_path / "letter.docx"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("word/document.xml", document_xml)
        result = text_extractors.extract_text(path)
        self.assertIn("Base Income: $85,000", result.text)
        self.assertIn("Employer: Acme Pty Ltd", result.text)

    def test_xlsx_extraction(self):
        shared_strings = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<sst xmlns="ns" count="1" uniqueCount="1"><si><t>Base Income</t></si></sst>'
        )
        sheet_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="ns"><sheetData>'
            '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1"><v>85000</v></c></row>'
            '</sheetData></worksheet>'
        )
        path = self.tmp_path / "book.xlsx"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("xl/sharedStrings.xml", shared_strings)
            zf.writestr("xl/worksheets/sheet1.xml", sheet_xml)
        result = text_extractors.extract_text(path)
        self.assertIn("Base Income, 85000", result.text)

    def test_pdf_blank_page_warns_ocr(self):
        from pypdf import PdfWriter

        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        path = self.tmp_path / "scan.pdf"
        with open(path, "wb") as fh:
            writer.write(fh)
        result = text_extractors.extract_text(path)
        self.assertTrue(any("OCR may be required" in w for w in result.warnings))

    def test_unsupported_extension(self):
        path = self._write("file.xyz", b"whatever")
        result = text_extractors.extract_text(path)
        self.assertEqual(result.text, "")
        self.assertTrue(result.warnings)

    def test_cap_length(self):
        path = self._write("big.txt", ("a" * (text_extractors.MAX_CHARS + 5000)).encode("utf-8"))
        result = text_extractors.extract_text(path)
        self.assertEqual(len(result.text), text_extractors.MAX_CHARS)


if __name__ == "__main__":
    unittest.main()
