"""Retrieval quality against expected sources (offline datasets only)."""

from collections.abc import Sequence


def first_hit_rank(retrieved: Sequence[str], expected: set[str]) -> int | None:
    """1-based rank of the first retrieved item that is expected, or None."""
    for rank, item in enumerate(retrieved, start=1):
        if item in expected:
            return rank
    return None


def hit_at_k(retrieved: Sequence[str], expected: set[str], k: int) -> float:
    rank = first_hit_rank(retrieved, expected)
    return 1.0 if rank is not None and rank <= k else 0.0


def reciprocal_rank(retrieved: Sequence[str], expected: set[str]) -> float:
    rank = first_hit_rank(retrieved, expected)
    return 1.0 / rank if rank else 0.0
