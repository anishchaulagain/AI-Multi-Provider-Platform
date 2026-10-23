"""BM25 sparse vectors for hybrid search.

Same scheme as fastembed's `Qdrant/bm25` model (lower-case word tokens, English
stop-word removal, Snowball stemming, mmh3 token ids, BM25 term-frequency weights),
without the onnxruntime dependency. IDF is applied by Qdrant (`Modifier.IDF`), so
documents and queries only need term weights.
"""

import re
from collections import Counter

import mmh3
from py_rust_stemmers import SnowballStemmer

from platform_rag.store import SparseVector

# NLTK English stop words (the list fastembed ships for Qdrant/bm25).
STOP_WORDS = frozenset(
    """a about above after again against ain all am an and any are aren aren't as at be
    because been before being below between both but by can couldn couldn't d did didn
    didn't do does doesn doesn't doing don don't down during each few for from further had
    hadn hadn't has hasn hasn't have haven haven't having he her here hers herself him
    himself his how i if in into is isn isn't it it's its itself just ll m ma me mightn
    mightn't more most mustn mustn't my myself needn needn't no nor not now o of off on once
    only or other our ours ourselves out over own re s same shan shan't she she's should
    should've shouldn shouldn't so some such t than that that'll the their theirs them
    themselves then there these they this those through to too under until up ve very was
    wasn wasn't we were weren weren't what when where which while who whom why will with won
    won't wouldn wouldn't y you you'd you'll you're you've your yours yourself
    yourselves""".split()
)

_TOKEN = re.compile(r"\w+", re.UNICODE)


class Bm25Encoder:
    def __init__(
        self,
        *,
        k: float = 1.2,
        b: float = 0.75,
        avg_len: float = 256.0,
        language: str = "english",
        max_token_len: int = 40,
    ) -> None:
        self.k = k
        self.b = b
        self.avg_len = avg_len
        self._stemmer = SnowballStemmer(language)
        self._max_token_len = max_token_len

    def tokens(self, text: str) -> list[str]:
        words = [
            w
            for w in _TOKEN.findall(text.lower())
            if w not in STOP_WORDS and len(w) <= self._max_token_len
        ]
        return [s for s in self._stemmer.stem_words(words) if s]

    @staticmethod
    def token_id(token: str) -> int:
        return abs(mmh3.hash(token))

    def _ids(self, tokens: list[str]) -> Counter[int]:
        return Counter(self.token_id(t) for t in tokens)

    def encode_document(self, text: str) -> SparseVector:
        tokens = self.tokens(text)
        doc_len = len(tokens)
        counts = self._ids(tokens)
        norm = self.k * (1 - self.b + self.b * doc_len / self.avg_len)
        indices = sorted(counts)
        return SparseVector(
            indices=indices,
            values=[counts[i] * (self.k + 1) / (counts[i] + norm) for i in indices],
        )

    def encode_query(self, text: str) -> SparseVector:
        indices = sorted(self._ids(self.tokens(text)))
        return SparseVector(indices=indices, values=[1.0] * len(indices))
