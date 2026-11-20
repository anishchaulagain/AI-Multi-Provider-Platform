"""Token counting with tiktoken, cached per text.

Exact counts need each model's own tokenizer; for budgeting, one BPE encoding per
model family is close enough (within a few percent). Falls back to ~4 chars/token
when the encoding can't be loaded (it downloads once, then lives in the tiktoken cache).
"""

import hashlib
from collections import OrderedDict
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

DEFAULT_ENCODING = "o200k_base"

# Model-family hints -> encoding. Anything unknown uses the default.
_FAMILY_ENCODINGS = {
    "gpt-oss": "o200k_base",
    "openai": "o200k_base",
    "llama": "cl100k_base",
    "qwen": "cl100k_base",
    "mistral": "cl100k_base",
}


def encoding_for(model: str | None) -> str:
    name = (model or "").lower()
    for hint, encoding in _FAMILY_ENCODINGS.items():
        if hint in name:
            return encoding
    return DEFAULT_ENCODING


class TokenCounter:
    def __init__(self, encoding: str = DEFAULT_ENCODING, *, cache_size: int = 4096) -> None:
        self.encoding_name = encoding
        self._encoding: Any = None
        self._failed = False
        self._cache: OrderedDict[str, int] = OrderedDict()
        self._cache_size = cache_size

    def _encoder(self) -> Any:
        if self._encoding is None and not self._failed:
            try:
                import tiktoken

                self._encoding = tiktoken.get_encoding(self.encoding_name)
            except Exception as exc:  # noqa: BLE001 - offline without a cached encoding
                logger.warning("tokenizer_unavailable_using_estimate", error=repr(exc))
                self._failed = True
        return self._encoding

    def count(self, text: str) -> int:
        if not text:
            return 0
        key = hashlib.blake2b(text.encode(), digest_size=12).hexdigest()
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached
        encoder = self._encoder()
        value = (
            len(encoder.encode(text, disallowed_special=())) if encoder else max(1, len(text) // 4)
        )
        self._cache[key] = value
        if len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return value

    def truncate(self, text: str, max_tokens: int, *, keep_tail: bool = False) -> str:
        """Cut `text` to at most `max_tokens`, marking the cut. With keep_tail, keep
        the head and the tail (useful for long messages whose ending matters)."""
        if max_tokens <= 0:
            return ""
        if self.count(text) <= max_tokens:
            return text
        marker = " … " if keep_tail else " …"
        budget = max(1, max_tokens - 2)
        encoder = self._encoder()
        if encoder is None:
            chars = budget * 4
            if keep_tail:
                return text[: chars // 2] + marker + text[-chars // 2 :]
            return text[:chars] + marker
        ids = encoder.encode(text, disallowed_special=())
        if keep_tail:
            half = budget // 2
            return str(encoder.decode(ids[:half])) + marker + str(encoder.decode(ids[-half:]))
        return str(encoder.decode(ids[:budget])) + marker
