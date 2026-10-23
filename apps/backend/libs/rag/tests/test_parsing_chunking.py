import pytest
from conftest import make_docx, make_pdf

from platform_rag.formats import DocumentParseError, UnsupportedFormatError, detect_format
from platform_rag.ingest.chunking import chunk_document
from platform_rag.ingest.parsing import parse
from platform_rag.ingest.sparse import Bm25Encoder

# ---------- format detection ----------


@pytest.mark.parametrize(
    ("filename", "data", "expected"),
    [
        ("a.pdf", b"%PDF-1.7 ...", "pdf"),
        ("a.DOCX", b"PK\x03\x04rest", "docx"),
        ("notes.md", b"# hi", "markdown"),
        ("page.htm", b"<html></html>", "html"),
        ("x.txt", "héllo".encode(), "text"),
    ],
)
def test_detect_format(filename: str, data: bytes, expected: str) -> None:
    assert detect_format(filename, data) == expected


@pytest.mark.parametrize(
    ("filename", "data"),
    [
        ("evil.exe", b"MZ"),
        ("fake.pdf", b"<html>not a pdf</html>"),
        ("fake.docx", b"%PDF-1.4"),
        ("bin.txt", b"abc\x00\x01\x02"),
        ("latin.txt", b"\xff\xfe\xfa invalid utf8 \xc3\x28"),
    ],
)
def test_detect_format_rejects(filename: str, data: bytes) -> None:
    with pytest.raises(UnsupportedFormatError):
        detect_format(filename, data)


# ---------- parsing ----------


def test_pdf_pages_title_and_outline() -> None:
    data = make_pdf(
        ["Introduction text about retrieval.", "Details about chunking and embeddings."],
        title="RAG Guide",
        toc=[[1, "Intro", 1], [1, "Details", 2]],
    )
    doc = parse(data, "guide.pdf")
    assert doc.format == "pdf"
    assert doc.title == "RAG Guide"
    assert doc.page_count == 2
    assert [(s.page, s.section) for s in doc.sections] == [(1, "Intro"), (2, "Details")]
    assert "chunking" in doc.sections[1].text


def test_corrupt_pdf_is_a_parse_error() -> None:
    with pytest.raises(DocumentParseError, match="Corrupt|unreadable|Could not"):
        parse(b"%PDF-1.4\n garbage garbage", "broken.pdf")


def test_pdf_without_text_is_a_parse_error() -> None:
    with pytest.raises(DocumentParseError, match="No extractable text"):
        parse(make_pdf(["", ""]), "scan.pdf")


def test_docx_becomes_markdown() -> None:
    doc = parse(make_docx(), "handbook.docx")
    assert doc.is_markdown
    assert doc.title == "Handbook"
    assert "# Onboarding" in doc.text
    assert "## Security" in doc.text
    assert "| VPN | IT |" in doc.text


def test_html_drops_scripts_and_keeps_structure() -> None:
    html = b"""<html><head><title>Docs</title><script>var x=1;</script></head>
    <body><nav>menu</nav><h1>Setup</h1><p>Install the CLI.</p>
    <ul><li>Step one</li><li>Step two</li></ul></body></html>"""
    doc = parse(html, "docs.html")
    assert doc.title == "Docs"
    assert "var x" not in doc.text and "menu" not in doc.text
    assert "# Setup" in doc.text and "- Step two" in doc.text


def test_text_title_falls_back_to_first_line() -> None:
    doc = parse(b"Quarterly report\n\nRevenue grew.", "q3.txt")
    assert doc.title == "Quarterly report"


def test_empty_file_is_a_parse_error() -> None:
    with pytest.raises(DocumentParseError):
        parse(b"   \n  ", "empty.md")


# ---------- chunking ----------


def test_markdown_chunks_carry_section_paths() -> None:
    md = b"# Guide\n\nIntro text.\n\n## Install\n\nRun the installer.\n\n## Usage\n\nCall the API."
    chunks = chunk_document(parse(md, "guide.md"), chunk_size=128, chunk_overlap=0)
    by_section = {c.section: c.text for c in chunks}
    assert "Guide > Install" in by_section
    assert "Run the installer" in by_section["Guide > Install"]
    assert [c.index for c in chunks] == list(range(len(chunks)))


def test_chunks_respect_size_and_never_cross_pages() -> None:
    long_page = " ".join(f"Sentence number {i} talks about vector search." for i in range(300))
    doc = parse(make_pdf([long_page[:3000], "Second page only."]), "long.pdf")
    chunks = chunk_document(doc, chunk_size=64, chunk_overlap=8)
    assert len(chunks) > 3
    assert {c.page for c in chunks} == {1, 2}
    assert [c.text for c in chunks if c.page == 2] == ["Second page only."]


# ---------- sparse ----------


def test_bm25_stems_and_drops_stop_words() -> None:
    encoder = Bm25Encoder()
    assert encoder.tokens("The runners were running quickly") == ["runner", "run", "quick"]
    a = encoder.encode_document("running runs")
    b = encoder.encode_query("run")
    assert b.indices[0] in a.indices
    assert len(a.indices) == 1 and a.values[0] > b.values[0]  # tf=2 outweighs one query hit
