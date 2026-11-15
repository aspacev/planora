"""Turning an uploaded specification into plain text.

The intake works on text: the model reads it, and the person reads it too —
the extracted text is shown before anything is sent to the model, because
extraction is lossy (a scanned PDF yields nothing, a table yields a column of
cells) and a person must see what the model is about to be given.

Three families of files are read: plain text (.txt, .md), Word (.docx) and PDF.
The old binary .doc is refused by name rather than guessed at: reading it
properly takes a converter, and a half-read specification is worse than a
request to save it as .docx.

Everything here runs on a file from the outside, so every reader is bounded:
by the size of the upload (the route), by the unpacked size of the archive
member (docx is a zip, and a zip can unpack a thousandfold), by the number of
PDF pages, and by the amount of text produced.
"""

import io
import re
import zipfile
from xml.etree import ElementTree

from app.config import get_settings


class SpecError(Exception):
    """A refusal with a machine code; the client turns the code into words."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


#: The upload route's path. Named here because two places must agree on it: the
#: router that serves it and the body-size middleware that gives it a larger
#: ceiling than every other route.
UPLOAD_PATH = "/api/ai/spec/extract"

_WORD_NAMESPACE = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_DOCUMENT_MEMBER = "word/document.xml"
#: The unpacked ceiling of the document part. A specification of a thousand
#: pages is a few megabytes of XML; beyond this it is not a document but a bomb.
_MAX_DOCUMENT_XML_BYTES = 30_000_000
_MAX_PDF_PAGES = 300
#: How far past the text ceiling extraction keeps going before giving up. The
#: text is returned even when it is over the ceiling, so that a person can trim
#: it in the field — but not when it is over by an order of magnitude.
_OVERSHOOT = 10


def extract(filename: str, data: bytes) -> str:
    """The text of an uploaded file, cleaned. Raises `SpecError` on anything unreadable."""
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if extension in {"txt", "md"}:
        text = _from_plain(data)
    elif extension == "docx":
        text = _from_docx(data)
    elif extension == "pdf":
        text = _from_pdf(data)
    else:
        raise SpecError("spec_unsupported_format")

    text = clean(text)
    if not text:
        # A scanned PDF or a document of pictures: the file is fine and there is
        # nothing in it to read. A distinct code, because the remedy is distinct —
        # paste the text by hand.
        raise SpecError("spec_empty")
    if len(text) > get_settings().ai_max_spec_chars * _OVERSHOOT:
        raise SpecError("spec_too_long")
    return text


def clean(text: str) -> str:
    """Whitespace brought to order: the model pays for every character.

    NUL is removed because Postgres refuses it in a text column, and the session
    would fail to save over a byte nobody can see.
    """
    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t ]+", " ", line).strip() for line in text.split("\n")]
    # Runs of blank lines collapse to one: paragraphs stay separated, and the
    # page breaks of a PDF stop costing tokens.
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def checked(text: str) -> str:
    """The specification as it goes into a session: cleaned and within the ceiling.

    The ceiling is refused rather than cut to: a specification silently truncated
    in the middle loses its last requirements, and the plan built from it looks
    complete.
    """
    text = clean(text)
    if len(text) > get_settings().ai_max_spec_chars:
        raise SpecError("spec_too_long")
    return text


def _from_plain(data: bytes) -> str:
    # UTF-8 first, then UTF-16 by its byte-order mark, then Windows-1251: a
    # specification written in Russian in Notepad years ago is in exactly that,
    # and as UTF-8 it fails to decode rather than decoding into garbage.
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        encodings = ("utf-16",)
    else:
        encodings = ("utf-8-sig", "cp1251")
    for encoding in encodings:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise SpecError("spec_unreadable")


def _from_docx(data: bytes) -> str:
    """Paragraphs of a Word document, in document order, table cells included.

    Read with the standard library: a .docx is a zip with one XML part that
    matters, and the text is the `w:t` runs inside `w:p` paragraphs. A library
    would add a dependency for the sake of a dozen lines.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            info = archive.getinfo(_DOCUMENT_MEMBER)
            if info.file_size > _MAX_DOCUMENT_XML_BYTES:
                raise SpecError("spec_too_long")
            # The declared size is the archive's own claim; reading one byte past
            # the ceiling catches an archive that lies about it.
            with archive.open(info) as member:
                xml = member.read(_MAX_DOCUMENT_XML_BYTES + 1)
    except (zipfile.BadZipFile, KeyError, OSError) as error:
        raise SpecError("spec_unreadable") from error
    if len(xml) > _MAX_DOCUMENT_XML_BYTES:
        raise SpecError("spec_too_long")
    # A Word document never carries a DTD. One that does is built to make the
    # parser expand entities, and it is refused before the parser sees it.
    if b"<!DOCTYPE" in xml or b"<!ENTITY" in xml:
        raise SpecError("spec_unreadable")

    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as error:
        raise SpecError("spec_unreadable") from error

    paragraphs: list[str] = []
    for paragraph in root.iter(f"{_WORD_NAMESPACE}p"):
        parts: list[str] = []
        for node in paragraph.iter():
            if node.tag == f"{_WORD_NAMESPACE}t":
                parts.append(node.text or "")
            elif node.tag in {f"{_WORD_NAMESPACE}tab", f"{_WORD_NAMESPACE}br"}:
                parts.append(" ")
        paragraphs.append("".join(parts))
    return "\n".join(paragraphs)


def _from_pdf(data: bytes) -> str:
    # Imported here: pypdf is needed by this one function, and the rest of the
    # intake must not fail to import if the package is absent from a build.
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError

    ceiling = get_settings().ai_max_spec_chars * _OVERSHOOT
    pages: list[str] = []
    total = 0
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise SpecError("spec_unreadable")
        for index, page in enumerate(reader.pages):
            if index >= _MAX_PDF_PAGES:
                raise SpecError("spec_too_long")
            text = page.extract_text() or ""
            pages.append(text)
            total += len(text)
            # Stop as soon as the answer is known: every further page is parsing
            # time spent on a refusal.
            if total > ceiling:
                raise SpecError("spec_too_long")
    except SpecError:
        raise
    except (PyPdfError, ValueError, KeyError, TypeError, OSError, RecursionError) as error:
        # pypdf reports a damaged file with whatever exception the damaged part
        # raised; all of them mean one thing to the person uploading.
        raise SpecError("spec_unreadable") from error
    return "\n\n".join(pages)
