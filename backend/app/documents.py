"""The requirements document a project is created from: its text and its storage.

Everything here is independent of the model: what kind of file arrived, what
text is in it, how that text is cut into parts for reading. The steps that talk
to the model live in app/ai/intake.py.

The format is decided by the file's contents rather than by its extension or
the Content-Type the browser sent: both are whatever the sender says they are,
and a renamed archive must not reach a parser that trusts the name.
"""

import hashlib
import io
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree

from sqlalchemy import delete, select
from sqlalchemy.orm import Session as DbSession

from app.config import get_settings
from app.models import AiSession, Organization, ProjectDocument, User

PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
TEXT_MIME = "text/plain"
MARKDOWN_MIME = "text/markdown"

#: How much text one request to the model reads. A whole document in one request
#: would not fit into a serverless function's thirty seconds; with the ceiling
#: of 300 000 characters this gives at most eight parts — under the limit of ten
#: model calls a minute.
PART_CHARS = 40_000

#: A document whose session nobody came back to for this long is swept.
ABANDONED_AFTER = timedelta(days=30)

#: The ceiling on the unpacked XML of a DOCX. The archive is bounded by
#: MAX_DOCUMENT_BYTES, but a few kilobytes of zip unpack into gigabytes; the
#: declared size is checked before a single byte is inflated.
_MAX_DOCX_XML_BYTES = 50_000_000

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class DocumentError(Exception):
    """A refusal to accept a document, named by a machine code.

    The code is the whole message: the interface translates it by dictionary,
    exactly as it does for a mutation refusal.
    """

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Extracted:
    content_type: str
    text: str


# --- extraction --------------------------------------------------------------


def extract(data: bytes, filename: str = "") -> Extracted:
    """The document's text, or a refusal that says what is wrong with the file.

    The size is checked first and before any parsing: a parser must not be the
    thing that discovers a file is too big.
    """
    settings = get_settings()
    if len(data) > settings.max_document_bytes:
        raise DocumentError("document_too_large")

    if data.startswith(b"%PDF-"):
        content_type, text = PDF_MIME, _pdf_text(data)
    elif data.startswith(b"PK\x03\x04"):
        content_type, text = DOCX_MIME, _docx_text(data)
    else:
        content_type, text = _plain_text(data, filename)

    text = _tidy(text)
    if not text:
        # A scan is a PDF of pictures: the file is fine, there is simply nothing
        # to read in it. Named separately from "unreadable" — a person fixes the
        # two differently.
        raise DocumentError("document_no_text")
    if len(text) > settings.ai_max_document_chars:
        raise DocumentError("document_too_long")
    return Extracted(content_type=content_type, text=text)


def _pdf_text(data: bytes) -> str:
    # The import is local: pypdf is needed by one branch of one route, and
    # loading it at start-up is paid by every cold start of the serverless
    # function.
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError

    try:
        reader = PdfReader(io.BytesIO(data))
        # An owner-password-only PDF opens with an empty password; one that
        # really asks for a password does not, and there is nobody to ask.
        if reader.is_encrypted and not reader.decrypt(""):
            raise DocumentError("document_unreadable")
        pages = [page.extract_text() or "" for page in reader.pages]
    except DocumentError:
        raise
    except (PyPdfError, ValueError, KeyError, TypeError, IndexError, OSError, RecursionError):
        # pypdf raises a wide family on a damaged file — and anything it raises
        # here means the same thing to the person: this file cannot be read.
        raise DocumentError("document_unreadable")
    return "\n\n".join(pages)


def _docx_text(data: bytes) -> str:
    """A DOCX is a zip archive with XML inside — the standard library reads both.

    Headings stay headings (a line starting with "#"): section references are
    built from them, and a flattened document would lose the only landmarks its
    author left.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = set(archive.namelist())
            if "word/document.xml" not in names:
                # A zip, but not a Word document: an .xlsx, a .jar, an archive.
                raise DocumentError("document_type_unsupported")
            body = _read_member(archive, "word/document.xml")
            styles = _read_member(archive, "word/styles.xml") if "word/styles.xml" in names else b""
    except zipfile.BadZipFile:
        raise DocumentError("document_unreadable")
    except RuntimeError:
        # zipfile reports an encrypted member this way.
        raise DocumentError("document_unreadable")

    try:
        root = ElementTree.fromstring(body)
        headings = _heading_styles(ElementTree.fromstring(styles)) if styles else {}
    except ElementTree.ParseError:
        raise DocumentError("document_unreadable")

    lines: list[str] = []
    for paragraph in root.iter(f"{_W}p"):
        text = _paragraph_text(paragraph).strip()
        if not text:
            continue
        level = _heading_level(paragraph, headings)
        lines.append(f"{'#' * level} {text}" if level else text)
    return "\n\n".join(lines)


def _read_member(archive: zipfile.ZipFile, name: str) -> bytes:
    if archive.getinfo(name).file_size > _MAX_DOCX_XML_BYTES:
        raise DocumentError("document_unreadable")
    return archive.read(name)


def _paragraph_text(paragraph: ElementTree.Element) -> str:
    pieces: list[str] = []
    for node in paragraph.iter():
        if node.tag == f"{_W}t":
            pieces.append(node.text or "")
        elif node.tag == f"{_W}tab":
            pieces.append("\t")
        elif node.tag in {f"{_W}br", f"{_W}cr"}:
            pieces.append("\n")
    return "".join(pieces)


def _heading_styles(styles: ElementTree.Element) -> dict[str, int]:
    """Style id -> heading level.

    The id is not reliable by itself: an English Word writes "Heading1", a
    Russian one writes "1" — but both give the style the name "heading 1", and
    that name is what the format fixes.
    """
    levels: dict[str, int] = {}
    for style in styles.iter(f"{_W}style"):
        style_id = style.get(f"{_W}styleId")
        name = style.find(f"{_W}name")
        label = (name.get(f"{_W}val") if name is not None else "") or ""
        match = re.fullmatch(r"heading\s*(\d)", label.strip().lower())
        if style_id and match:
            levels[style_id] = int(match.group(1))
        elif style_id and label.strip().lower() == "title":
            levels[style_id] = 1
    return levels


def _heading_level(paragraph: ElementTree.Element, headings: dict[str, int]) -> int:
    properties = paragraph.find(f"{_W}pPr")
    if properties is None:
        return 0
    style = properties.find(f"{_W}pStyle")
    if style is not None:
        style_id = style.get(f"{_W}val") or ""
        if style_id in headings:
            return headings[style_id]
        # A document without styles.xml (or produced by a converter) still
        # usually keeps the conventional id.
        match = re.fullmatch(r"[Hh]eading\s*(\d)", style_id)
        if match:
            return int(match.group(1))
    outline = properties.find(f"{_W}outlineLvl")
    if outline is not None and (outline.get(f"{_W}val") or "").isdigit():
        return min(int(outline.get(f"{_W}val")) + 1, 6)
    return 0


def _plain_text(data: bytes, filename: str) -> tuple[str, str]:
    """TXT and MD: the bytes as they are, in UTF-8.

    Anything that is not valid UTF-8 text is "a format not from the list" — an
    image, an old .doc, a spreadsheet all end up here, and none of them is text.
    """
    if b"\x00" in data:
        raise DocumentError("document_type_unsupported")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise DocumentError("document_type_unsupported")
    markdown = filename.lower().endswith((".md", ".markdown"))
    return (MARKDOWN_MIME if markdown else TEXT_MIME), text


def _tidy(text: str) -> str:
    """Line endings unified, trailing spaces and runs of blank lines removed.

    Not cosmetics: the text is paid for by the character, and a PDF's extraction
    is generous with empty lines. NUL cannot be stored in a Postgres text column
    at all.
    """
    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# --- parts -------------------------------------------------------------------


def split_parts(text: str, size: int = PART_CHARS) -> list[str]:
    """The text cut into parts of about `size` characters, along paragraph borders.

    Deterministic: the session remembers only how many parts have been read, so
    the same text must always be cut the same way. A paragraph longer than a
    whole part (a document without a single blank line) is cut by length — a
    worse border, but a part must not outgrow one request.
    """
    parts: list[str] = []
    current: list[str] = []
    length = 0

    def flush() -> None:
        nonlocal current, length
        if current:
            parts.append("\n\n".join(current))
            current, length = [], 0

    for paragraph in text.split("\n\n"):
        for piece in (paragraph[i : i + size] for i in range(0, len(paragraph), size)):
            # +2 — the blank line the pieces are joined with.
            if current and length + len(piece) + 2 > size:
                flush()
            current.append(piece)
            length += len(piece) + 2
    flush()
    return parts


def estimate_tokens(chars: int) -> int:
    """How many tokens reading this much text will cost, estimated from above.

    A token is about four characters of English and about two of Russian or
    Azerbaijani; the estimate takes the expensive case. It is used to refuse a
    document that will not fit into what is left of today's budget before the
    model is called at all — an overestimate refuses a little early, an
    underestimate would stop the reading halfway with the money already spent.
    """
    return chars // 2 + 1


# --- storage -----------------------------------------------------------------


def clean_filename(raw: str) -> str:
    """A name fit to store and to put into Content-Disposition.

    Only the last path segment, no control characters, at most the column's
    length. An empty result gets a neutral name: a document without a name is
    still a document.
    """
    name = re.split(r"[\\/]", raw or "")[-1]
    name = "".join(ch for ch in name if ch.isprintable()).strip().strip(".")
    return name[:255] or "document"


def store(
    db: DbSession, *, org: Organization, user: User, filename: str, data: bytes, extracted: Extracted
) -> ProjectDocument:
    document = ProjectDocument(
        org_id=org.id,
        filename=clean_filename(filename),
        content_type=extracted.content_type,
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        content=data,
        extracted_text=extracted.text,
        uploaded_by=user.id,
    )
    db.add(document)
    db.flush()
    return document


def sweep_abandoned(db: DbSession, org: Organization) -> int:
    """Removes the documents of intakes nobody finished, older than thirty days.

    Called on every upload in the same organization: the product has no
    scheduler, so the tidying is done in passing by whoever comes next. Only
    documents that never became a project's are touched — an applied one has
    `project_id` and lives as long as the project does.

    The age is the upload's: a session keeps no "last touched" stamp, and an
    intake still unfinished a month after its upload is abandoned by any
    reasonable reading.
    """
    cutoff = datetime.now(timezone.utc) - ABANDONED_AFTER
    stale = select(ProjectDocument.id).where(
        ProjectDocument.org_id == org.id,
        ProjectDocument.project_id.is_(None),
        ProjectDocument.created_at < cutoff,
    )
    # The sessions go first and in the same sweep: without its document a
    # half-read session can never be finished, and it would otherwise answer
    # its address forever.
    db.execute(
        delete(AiSession).where(
            AiSession.document_id.in_(stale), AiSession.applied_batch_id.is_(None)
        )
    )
    result = db.execute(delete(ProjectDocument).where(ProjectDocument.id.in_(stale)))
    return result.rowcount or 0


def project_document(db: DbSession, project_id) -> ProjectDocument | None:
    return db.scalar(select(ProjectDocument).where(ProjectDocument.project_id == project_id))
