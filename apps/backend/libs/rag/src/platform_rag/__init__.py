"""RAG building blocks.

The base package (formats, store) is light and used by the API; `platform_rag.ingest`
needs the `[ingest]` extra (LlamaIndex, PyMuPDF, ...), installed by the worker.
"""
