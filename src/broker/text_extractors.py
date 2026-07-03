"""Plain-text extraction for the document types brokers receive.

Every extractor returns an ``ExtractionResult`` with ``text`` (capped at
``MAX_CHARS``) and a list of human-readable ``warnings`` (e.g. "OCR may be
required" for image-only PDF pages). Extraction never raises for a
malformed/unsupported file; it returns an empty result with a warning
instead, so a single bad document never aborts a run.
"""
from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from email import message_from_bytes, policy
from email.message import Message
from html.parser import HTMLParser
from pathlib import Path
from typing import List

MAX_CHARS = 250_000

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+")


class _TextOnlyHTMLParser(HTMLParser):
    """Collects text nodes only, skipping <script>/<style> content.

    Unlike a naive ``<[^>]+>`` regex, a real parser doesn't treat a stray
    unescaped '<' in body text (e.g. "Income < $85,000") as the start of a
    tag that swallows everything up to the next unrelated '>'.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self.parts: List[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0:
            self.parts.append(data)


@dataclass
class ExtractionResult:
    text: str
    warnings: List[str] = field(default_factory=list)


def _cap(text: str) -> str:
    return text[:MAX_CHARS]


def _strip_html(html: str) -> str:
    parser = _TextOnlyHTMLParser()
    try:
        parser.feed(html)
        parser.close()
        text = " ".join(parser.parts)
    except Exception:  # noqa: BLE001 - fall back to the naive regex on bizarre input
        text = _TAG_RE.sub(" ", html)
    text = re.sub(r"\s*\n\s*", "\n", text)
    text = _WS_RE.sub(" ", text)
    return text.strip()


def extract_pdf(path: Path) -> ExtractionResult:
    warnings: List[str] = []
    try:
        from pypdf import PdfReader
    except ImportError:
        return ExtractionResult("", ["pypdf is not installed; cannot read PDF"])
    try:
        reader = PdfReader(str(path))
    except Exception as exc:  # noqa: BLE001 - never abort a run on a bad file
        return ExtractionResult("", [f"Could not open PDF: {exc}"])
    parts = []
    for i, page in enumerate(reader.pages):
        try:
            page_text = page.extract_text() or ""
        except Exception:  # noqa: BLE001
            page_text = ""
        if not page_text.strip():
            warnings.append(f"OCR may be required (page {i + 1} has no extractable text)")
        parts.append(page_text)
    return ExtractionResult(_cap("\n".join(parts)), warnings)


def extract_docx(path: Path) -> ExtractionResult:
    try:
        with zipfile.ZipFile(path) as zf:
            xml = zf.read("word/document.xml").decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        return ExtractionResult("", [f"Could not open DOCX: {exc}"])
    xml = xml.replace("</w:p>", "\n").replace("</w:tr>", "\n")
    text = _TAG_RE.sub("", xml)
    text = re.sub(r"\n{2,}", "\n", text)
    return ExtractionResult(_cap(text.strip()))


def extract_xlsx(path: Path) -> ExtractionResult:
    try:
        with zipfile.ZipFile(path) as zf:
            shared: List[str] = []
            if "xl/sharedStrings.xml" in zf.namelist():
                xml = zf.read("xl/sharedStrings.xml").decode("utf-8", errors="replace")
                for match in re.finditer(r"<t[^>]*>(.*?)</t>", xml, re.S):
                    shared.append(_TAG_RE.sub("", match.group(1)))
            sheet_names = sorted(
                n for n in zf.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n)
            )
            rows: List[str] = []
            for name in sheet_names:
                sheet_xml = zf.read(name).decode("utf-8", errors="replace")
                for row_match in re.finditer(r"<row[^>]*>(.*?)</row>", sheet_xml, re.S):
                    cells = []
                    for cell_match in re.finditer(
                        r"<c\b([^>]*)>(.*?)</c>", row_match.group(1), re.S
                    ):
                        attrs, cell_body = cell_match.groups()
                        type_match = re.search(r'\bt="([^"]*)"', attrs)
                        cell_type = type_match.group(1) if type_match else None
                        value_match = re.search(r"<v>(.*?)</v>", cell_body, re.S)
                        if not value_match:
                            continue
                        raw = value_match.group(1)
                        if cell_type == "s":
                            try:
                                raw = shared[int(raw)]
                            except (ValueError, IndexError):
                                pass
                        cells.append(raw)
                    if cells:
                        rows.append(", ".join(cells))
            return ExtractionResult(_cap("\n".join(rows)))
    except Exception as exc:  # noqa: BLE001
        return ExtractionResult("", [f"Could not open XLSX: {exc}"])


def extract_csv(path: Path, delimiter: str = ",") -> ExtractionResult:
    try:
        raw = path.read_bytes().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        return ExtractionResult("", [f"Could not read CSV/TSV: {exc}"])
    rows = []
    try:
        reader = csv.reader(io.StringIO(raw), delimiter=delimiter)
        for row in reader:
            rows.append(", ".join(row))
    except Exception as exc:  # noqa: BLE001
        return ExtractionResult("", [f"Could not parse CSV/TSV: {exc}"])
    return ExtractionResult(_cap("\n".join(rows)))


def extract_plain_text(path: Path) -> ExtractionResult:
    try:
        text = path.read_bytes().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        return ExtractionResult("", [f"Could not read file: {exc}"])
    return ExtractionResult(_cap(text))


def extract_html(path: Path) -> ExtractionResult:
    try:
        html = path.read_bytes().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        return ExtractionResult("", [f"Could not read HTML: {exc}"])
    return ExtractionResult(_cap(_strip_html(html)))


def extract_json(path: Path) -> ExtractionResult:
    try:
        data = json.loads(path.read_bytes().decode("utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001
        return ExtractionResult("", [f"Could not parse JSON: {exc}"])
    return ExtractionResult(_cap(json.dumps(data, indent=2, ensure_ascii=False)))


def _eml_body_text(msg: Message) -> str:
    if msg.is_multipart():
        plain_parts = []
        html_parts = []
        for part in msg.walk():
            if part.is_multipart():
                continue
            content_type = part.get_content_type()
            try:
                content = part.get_content()
            except Exception:  # noqa: BLE001
                continue
            if content_type == "text/plain" and isinstance(content, str):
                plain_parts.append(content)
            elif content_type == "text/html" and isinstance(content, str):
                html_parts.append(content)
        if plain_parts:
            return "\n".join(plain_parts)
        if html_parts:
            return "\n".join(_strip_html(h) for h in html_parts)
        return ""
    try:
        content = msg.get_content()
    except Exception:  # noqa: BLE001
        return ""
    if not isinstance(content, str):
        return ""
    if msg.get_content_type() == "text/html":
        return _strip_html(content)
    return content


def extract_eml(path: Path) -> ExtractionResult:
    try:
        raw = path.read_bytes()
        msg = message_from_bytes(raw, policy=policy.default)
    except Exception as exc:  # noqa: BLE001
        return ExtractionResult("", [f"Could not parse EML: {exc}"])
    preamble = (
        f"From: {msg.get('From', '')}\n"
        f"Date: {msg.get('Date', '')}\n"
        f"Subject: {msg.get('Subject', '')}\n\n"
    )
    body = _eml_body_text(msg)
    return ExtractionResult(_cap(preamble + body))


_DISPATCH = {
    ".pdf": extract_pdf,
    ".docx": extract_docx,
    ".xlsx": extract_xlsx,
    ".csv": lambda p: extract_csv(p, ","),
    ".tsv": lambda p: extract_csv(p, "\t"),
    ".txt": extract_plain_text,
    ".md": extract_plain_text,
    ".html": extract_html,
    ".htm": extract_html,
    ".json": extract_json,
    ".eml": extract_eml,
}


def extract_text(path: Path) -> ExtractionResult:
    """Dispatch to the right extractor based on file extension."""
    path = Path(path)
    extractor = _DISPATCH.get(path.suffix.lower())
    if extractor is None:
        return ExtractionResult("", [f"Unsupported file type: {path.suffix or '(none)'}"])
    return extractor(path)
