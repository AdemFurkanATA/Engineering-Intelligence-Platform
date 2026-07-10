"""
TF-IDF ranking model with incremental updates and thread-safe access.

Theory
------
TF (Term Frequency)
    How often term t appears in document d, normalised by document length.
        tf(t, d) = count(t in d) / |d|

IDF (Inverse Document Frequency) — smooth variant
    How rare term t is across the entire corpus.
    Rare terms are more discriminative.
        idf(t) = log(N / (df(t) + 1)) + 1
    Where N = total documents, df(t) = documents containing t.
    The "+1" denominator prevents division by zero.
    The "+1" outside the log prevents IDF = 0 for universal terms.

TF-IDF weight
        w(t, d) = tf(t, d) * idf(t)

Cosine Similarity
    Measures the angle between the query vector and the document vector.
    Insensitive to document length, focuses on term distribution.
        cosine(q, d) = (q · d) / (|q| * |d|)

Thread Safety
-------------
TFIDFIndex is called from both the asyncio event loop AND from threads
via ThreadPoolExecutor (for the score() calls during search).
A threading.RLock protects all state mutations.
"""
import math
import threading
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Set


class TFIDFIndex:
    """
    Incremental TF-IDF index with cosine similarity scoring.

    Properties:
    - Incremental: adding/removing documents updates IDF lazily (dirty flag)
    - Thread-safe: threading.RLock protects all state mutations and reads
    - Memory-efficient: stores TF as {doc_id: {token: tf}} sparse dict

    Complexity:
    - add_document:    O(|tokens|)
    - remove_document: O(|vocab in doc|)
    - score:           O(|query_tokens|) after IDF is cached
    - recompute_idf:   O(|vocabulary|) — only triggered when dirty
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()  # re-entrant: allows same thread to acquire multiple times

        # TF scores: doc_id → {token → tf}
        self._tf: Dict[str, Dict[str, float]] = {}

        # Document frequency: token → set of doc_ids
        self._df: Dict[str, Set[str]] = defaultdict(set)

        # Cached IDF values (recomputed lazily on dirty)
        self._idf_cache: Dict[str, float] = {}
        self._idf_dirty: bool = True

        # Per-document L2 norms for cosine similarity (recomputed lazily)
        self._norms: Dict[str, float] = {}
        self._norms_dirty: Set[str] = set()

        self._n_docs: int = 0

    # ------------------------------------------------------------------
    # Writes (always under lock)
    # ------------------------------------------------------------------

    def add_document(self, doc_id: str, text: str) -> None:
        """
        Index a document. If doc_id already exists, re-indexes it.
        Marks IDF as dirty — will be lazily recomputed on next score().
        """
        tokens = _tokenize(text)
        if not tokens:
            return

        with self._lock:
            # Remove old version if reindexing
            if doc_id in self._tf:
                self._remove_locked(doc_id)

            counts = Counter(tokens)
            doc_len = len(tokens)

            # Compute and store TF
            self._tf[doc_id] = {
                token: count / doc_len
                for token, count in counts.items()
            }

            # Update document frequency
            for token in counts:
                self._df[token].add(doc_id)

            self._n_docs += 1
            self._idf_dirty = True
            self._norms_dirty.add(doc_id)

    def remove_document(self, doc_id: str) -> None:
        """Remove a document from the index."""
        with self._lock:
            self._remove_locked(doc_id)

    def _remove_locked(self, doc_id: str) -> None:
        """Internal remove — caller must hold self._lock."""
        if doc_id not in self._tf:
            return

        for token in self._tf[doc_id]:
            self._df[token].discard(doc_id)
            if not self._df[token]:
                del self._df[token]
                self._idf_cache.pop(token, None)

        del self._tf[doc_id]
        self._norms.pop(doc_id, None)
        self._norms_dirty.discard(doc_id)
        self._n_docs = max(0, self._n_docs - 1)
        self._idf_dirty = True

    # ------------------------------------------------------------------
    # IDF computation (lazy, under lock)
    # ------------------------------------------------------------------

    def _recompute_idf(self) -> None:
        """
        Recompute IDF for all vocabulary terms.
        Smooth IDF: idf(t) = log(N / (df(t) + 1)) + 1
        Called lazily when _idf_dirty is True.
        """
        n = max(self._n_docs, 1)
        self._idf_cache = {
            token: math.log(n / (len(doc_ids) + 1)) + 1.0
            for token, doc_ids in self._df.items()
        }
        self._idf_dirty = False

    def _recompute_norm(self, doc_id: str) -> None:
        """
        Compute L2 norm of the TF-IDF vector for doc_id.
        norm(d) = sqrt( sum( (tf(t,d) * idf(t))^2 for t in d ) )
        """
        tf = self._tf.get(doc_id, {})
        norm_sq = sum(
            (tf_val * self._idf_cache.get(token, 1.0)) ** 2
            for token, tf_val in tf.items()
        )
        self._norms[doc_id] = math.sqrt(norm_sq) if norm_sq > 0 else 1.0
        self._norms_dirty.discard(doc_id)

    # ------------------------------------------------------------------
    # Scoring (reads — under lock for IDF/norm refresh)
    # ------------------------------------------------------------------

    def score(self, doc_id: str, query_tokens: List[str]) -> float:
        """
        Compute cosine similarity between a query and a document.

        Formula:
            cosine(q, d) = Σ [q_tfidf(t) * d_tfidf(t)] / (norm_q * norm_d)

        Query model: uniform TF (1/|Q| per token), combined with IDF.
        """
        with self._lock:
            if doc_id not in self._tf:
                return 0.0

            if self._idf_dirty:
                self._recompute_idf()
            if doc_id in self._norms_dirty:
                self._recompute_norm(doc_id)

            tf_doc = self._tf[doc_id]
            q_len = max(len(query_tokens), 1)

            # Dot product of query and document TF-IDF vectors
            dot = 0.0
            for token in query_tokens:
                idf = self._idf_cache.get(token, 0.0)
                q_weight = (1.0 / q_len) * idf
                d_weight = tf_doc.get(token, 0.0) * idf
                dot += q_weight * d_weight

            # Query L2 norm
            q_norm = math.sqrt(
                sum(((1.0 / q_len) * self._idf_cache.get(t, 0.0)) ** 2 for t in query_tokens)
            ) or 1.0

            d_norm = self._norms.get(doc_id, 1.0)

            return dot / (q_norm * d_norm)

    def idf(self, token: str) -> float:
        """Return the IDF score for a single token."""
        with self._lock:
            if self._idf_dirty:
                self._recompute_idf()
            return self._idf_cache.get(token, 0.0)

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    @property
    def document_count(self) -> int:
        with self._lock:
            return self._n_docs

    @property
    def vocabulary_size(self) -> int:
        with self._lock:
            return len(self._df)

    def top_terms(self, n: int = 20) -> List[tuple]:
        """Return the n terms with the highest IDF (most discriminative)."""
        with self._lock:
            if self._idf_dirty:
                self._recompute_idf()
            ranked = sorted(self._idf_cache.items(), key=lambda x: x[1], reverse=True)
            return ranked[:n]


# ---------------------------------------------------------------------------
# Shared tokenizer (used by both TFIDFIndex and SearchEngine)
# ---------------------------------------------------------------------------

import re as _re

def _tokenize(text: str) -> List[str]:
    return _re.findall(r"[a-z0-9]+", text.lower())
