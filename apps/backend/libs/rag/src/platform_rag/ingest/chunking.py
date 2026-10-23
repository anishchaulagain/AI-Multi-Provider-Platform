"""ParsedDocument -> chunks, using LlamaIndex node parsers.

Markdown (incl. converted DOCX/HTML) is first split on headings with
`MarkdownNodeParser`, so every chunk knows its section path; each section/page is
then split with `SentenceSplitter`. Chunks never span two PDF pages, which keeps
page citations exact.
"""

from dataclasses import dataclass

from llama_index.core import Document as LlamaDocument
from llama_index.core.node_parser import MarkdownNodeParser, SentenceSplitter

from platform_rag.ingest.parsing import ParsedDocument, Section


@dataclass(frozen=True)
class Chunk:
    index: int
    text: str
    page: int | None
    section: str | None


def _markdown_sections(section: Section) -> list[Section]:
    nodes = MarkdownNodeParser().get_nodes_from_documents([LlamaDocument(text=section.text)])
    out: list[Section] = []
    for node in nodes:
        text = node.get_content()
        parents = [p for p in str(node.metadata.get("header_path", "")).split("/") if p]
        first = text.lstrip().splitlines()[0] if text.strip() else ""
        heading = first.lstrip("#").strip() if first.startswith("#") else None
        path = [*parents, heading] if heading else parents
        out.append(
            Section(text=text, page=section.page, section=" > ".join(path) or section.section)
        )
    return out


def chunk_document(
    doc: ParsedDocument, *, chunk_size: int = 512, chunk_overlap: int = 64
) -> list[Chunk]:
    splitter = SentenceSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
    sections: list[Section] = []
    for section in doc.sections:
        sections.extend(_markdown_sections(section) if doc.is_markdown else [section])

    chunks: list[Chunk] = []
    for section in sections:
        for text in splitter.split_text(section.text):
            text = text.strip()
            # Skip fragments that are only a heading or punctuation.
            if len(text) < 3 or not any(ch.isalnum() for ch in text):
                continue
            chunks.append(Chunk(len(chunks), text, section.page, section.section))
    return chunks
