"""Supported upload formats and content sniffing (no heavy deps: the API uses this)."""

from pathlib import PurePath
from typing import Literal

Format = Literal["pdf", "docx", "markdown", "html", "text"]

EXTENSIONS: dict[str, Format] = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".md": "markdown",
    ".markdown": "markdown",
    ".html": "html",
    ".htm": "html",
    ".txt": "text",
}

CONTENT_TYPES: dict[Format, str] = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "markdown": "text/markdown",
    "html": "text/html",
    "text": "text/plain",
}


class UnsupportedFormatError(ValueError):
    pass


class DocumentParseError(Exception):
    """The file is corrupt, empty, encrypted or otherwise unreadable. Not retryable."""


def detect_format(filename: str, data: bytes) -> Format:
    """Pick the format from the extension, then check the bytes actually match it."""
    ext = PurePath(filename).suffix.lower()
    fmt = EXTENSIONS.get(ext)
    if fmt is None:
        supported = ", ".join(sorted(EXTENSIONS))
        raise UnsupportedFormatError(f"Unsupported file type '{ext or filename}'. Use: {supported}")

    head = data[:1024]
    if fmt == "pdf" and not head.lstrip().startswith(b"%PDF-"):
        raise UnsupportedFormatError("File has a .pdf extension but is not a PDF")
    if fmt == "docx" and not head.startswith(b"PK\x03\x04"):
        raise UnsupportedFormatError("File has a .docx extension but is not a Word document")
    if fmt in ("markdown", "html", "text"):
        if b"\x00" in head:
            raise UnsupportedFormatError("Text file contains binary data")
        try:
            decode_text(data[:4096], strict=True)
        except UnicodeDecodeError as exc:
            raise UnsupportedFormatError("Text file is not valid UTF-8") from exc
    return fmt


def decode_text(data: bytes, *, strict: bool = False) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        if strict:
            # A multi-byte char may be cut at the sniffing boundary.
            return data[:-4].decode("utf-8-sig") if len(data) > 4 else data.decode("utf-8")
        return data.decode("latin-1")
