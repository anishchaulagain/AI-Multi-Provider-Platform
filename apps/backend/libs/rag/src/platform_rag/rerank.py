"""Cross-encoder reranking. `FastembedReranker` needs the `[rerank]` extra."""

import asyncio
from collections.abc import Sequence
from typing import Any, Protocol

import structlog

logger = structlog.get_logger(__name__)

DEFAULT_RERANK_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"


class Reranker(Protocol):
    name: str

    async def score(self, query: str, texts: Sequence[str]) -> list[float]:
        """Relevance score per text (higher is better)."""


class NoReranker:
    """Keeps the fused order: scores decrease with position."""

    name = "none"

    async def score(self, query: str, texts: Sequence[str]) -> list[float]:
        return [float(len(texts) - i) for i in range(len(texts))]


class FastembedReranker:
    """ONNX cross-encoder on CPU. The model downloads on first use (~90 MB for the
    default MiniLM; `BAAI/bge-reranker-base` is ~1 GB and more accurate)."""

    def __init__(self, model: str = DEFAULT_RERANK_MODEL, *, cache_dir: str | None = None) -> None:
        self.name = model
        self._cache_dir = cache_dir
        self._encoder: Any = None
        self._lock = asyncio.Lock()

    async def _load(self) -> Any:
        async with self._lock:
            if self._encoder is None:
                from fastembed.rerank.cross_encoder import TextCrossEncoder

                self._encoder = await asyncio.to_thread(
                    TextCrossEncoder, self.name, cache_dir=self._cache_dir
                )
                logger.info("reranker_loaded", model=self.name)
        return self._encoder

    async def warm_up(self) -> None:
        await self._load()

    async def score(self, query: str, texts: Sequence[str]) -> list[float]:
        if not texts:
            return []
        encoder = await self._load()
        return await asyncio.to_thread(
            lambda: [float(s) for s in encoder.rerank(query, list(texts))]
        )


def build_reranker(kind: str, model: str = DEFAULT_RERANK_MODEL, **kwargs: Any) -> Reranker:
    if kind == "fastembed":
        return FastembedReranker(model, **kwargs)
    if kind == "none":
        return NoReranker()
    raise ValueError(f"Unknown reranker '{kind}' (use 'fastembed' or 'none')")
