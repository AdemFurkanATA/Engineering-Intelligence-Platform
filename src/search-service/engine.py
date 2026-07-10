"""
SearchEngine — unified search engine for the Engineering Intelligence Platform.

Combines:
─────────────────────────────────────────────────────────────────────────────
┌─────────────────────┬────────────────────────────────────────────────────┐
│ Component           │ Role                                               │
├─────────────────────┼────────────────────────────────────────────────────┤
│ Trie (trie.py)      │ O(k) prefix search, autocomplete                   │
│ Fuzzy (fuzzy.py)    │ Levenshtein + Jaro-Winkler typo tolerance          │
│ TF-IDF (ranking.py) │ Cosine-similarity relevance ranking                │
│ Inverted Index      │ O(1) exact token → doc_id lookup                   │
│ asyncio.Lock        │ Async write serialisation (coroutine-level safety) │
│ threading.RLock     │ Thread-level safety (TFIDFIndex, read-path)        │
│ ThreadPoolExecutor  │ CPU-bound work (fuzzy match) off the event loop    │
│ asyncio.gather      │ Parallel indexing of multiple documents            │
└─────────────────────┴────────────────────────────────────────────────────┘

Concurrency model
─────────────────
asyncio operates in a single OS thread but allows concurrent coroutines via
cooperative multitasking (await).  Two concurrency hazards exist:

  1. Two coroutines trying to write the index simultaneously.
     → asyncio.Lock on all write paths guarantees mutual exclusion between
       coroutines without blocking the event loop.

  2. A ThreadPoolExecutor thread reading TFIDFIndex while asyncio writes it.
     → threading.RLock inside TFIDFIndex protects every read and write.

Read path (search) is lock-free for the inverted index and Trie because
asyncio is single-threaded: while a coroutine reads, no other coroutine
can modify state without first awaiting (and the write lock ensures writers
do not interleave).  TFIDFIndex reads still use the threading.RLock because
score() may be called from a thread.
"""
import asyncio
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple

from fuzzy import bulk_fuzzy_resolve
from ranking import TFIDFIndex, _tokenize
from trie import Trie

logger = logging.getLogger(__name__)

# CPU-bound work runs in this pool (fuzzy matching, heavy scoring)
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="search-cpu")


class SearchEngine:
    """
    Thread-safe, hybrid search engine.

    Index structures (all protected by asyncio.Lock for writes):
      _docs      — primary document store (id → metadata)
      _inverted  — inverted index (token → set of doc_ids)
      _trie      — Prefix Tree for prefix search and autocomplete
      _tfidf     — TF-IDF index for cosine-similarity ranking
    """

    def __init__(self) -> None:
        self._docs: Dict[str, dict] = {}
        self._inverted: Dict[str, Set[str]] = {}
        self._trie = Trie()
        self._tfidf = TFIDFIndex()

        # asyncio write lock — serialises concurrent coroutine writes
        self._write_lock = asyncio.Lock()

        self._stats_indexed: int = 0
        self._stats_removed: int = 0

    # ------------------------------------------------------------------
    # Index management
    # ------------------------------------------------------------------

    async def add(
        self,
        entry_id: str,
        entry_type: str,
        text: str,
        metadata: dict,
    ) -> None:
        """
        Add or update a document in all index structures.
        Protected by asyncio.Lock — safe for concurrent callers.
        """
        async with self._write_lock:
            # Remove stale version first (idempotent update)
            if entry_id in self._docs:
                self._remove_from_indexes(entry_id)

            tokens = _tokenize(text)
            unique_tokens = set(tokens)

            # Primary store
            self._docs[entry_id] = {
                "id": entry_id,
                "type": entry_type,
                "text": text,
                "metadata": metadata,
                "tokenCount": len(tokens),
                "uniqueTokens": len(unique_tokens),
                "indexedAt": datetime.utcnow().isoformat() + "Z",
            }

            # Inverted index — O(|unique_tokens|)
            for token in unique_tokens:
                self._inverted.setdefault(token, set()).add(entry_id)

            # Trie — O(sum of token lengths)
            for token in unique_tokens:
                self._trie.insert(token, entry_id)

            # TF-IDF — O(|tokens|); runs synchronously (fast, protected by asyncio.Lock)
            self._tfidf.add_document(entry_id, text)

            self._stats_indexed += 1
            logger.debug("Indexed id=%s type=%s tokens=%d", entry_id, entry_type, len(unique_tokens))

    async def add_many(self, entries: List[dict]) -> None:
        """
        Index multiple documents using asyncio.gather for concurrent ingestion.
        Each add() call waits for the write lock, so execution is serialised but
        scheduling is interleaved — demonstrates gather-based producer pattern.
        """
        await asyncio.gather(*[
            self.add(
                e["id"],
                e["type"],
                e["text"],
                e.get("metadata", {}),
            )
            for e in entries
        ])

    async def remove(self, entry_id: str) -> bool:
        """Remove a document from all index structures. Thread-safe."""
        async with self._write_lock:
            if entry_id not in self._docs:
                return False
            self._remove_from_indexes(entry_id)
            self._stats_removed += 1
            return True

    def _remove_from_indexes(self, entry_id: str) -> None:
        """
        Internal helper — removes entry_id from all structures.
        Caller MUST hold self._write_lock.
        """
        doc = self._docs.pop(entry_id, None)
        if doc is None:
            return

        tokens = set(_tokenize(doc["text"]))
        for token in tokens:
            bucket = self._inverted.get(token)
            if bucket:
                bucket.discard(entry_id)
                self._trie.remove(token, entry_id)
                if not bucket:
                    del self._inverted[token]

        self._tfidf.remove_document(entry_id)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    async def search(
        self,
        query: str,
        top_k: int = 10,
        entity_type: Optional[str] = None,
        fuzzy: bool = True,
        max_fuzzy_distance: int = 2,
    ) -> List[dict]:
        """
        Hybrid search pipeline:

        1. EXACT   — inverted index O(|tokens|)         → high precision
        2. PREFIX  — Trie           O(|tokens| * k)     → catches partial words
        3. FUZZY   — Levenshtein    O(|vocab| * m * n)  → catches typos
           (CPU-bound: offloaded to ThreadPoolExecutor)
        4. RANK    — TF-IDF cosine similarity           → relevance ordering
        5. BOOST   — prefix and exact match bonuses     → structural signals
        6. EXPLAIN — matchType field per result         → transparency

        Returns top_k results sorted by composite score descending.
        """
        if not query.strip() or not self._docs:
            return []

        tokens = _tokenize(query)
        if not tokens:
            return []

        # ── Phase 1: Candidate gathering ──────────────────────────────

        # Exact inverted index lookup — O(1) per token
        exact_candidates: Set[str] = set()
        for token in tokens:
            exact_candidates |= self._inverted.get(token, set())

        # Prefix Trie lookup — catches "pay" → "payment"
        prefix_candidates: Set[str] = set()
        for token in tokens:
            prefix_candidates |= self._trie.search_prefix(token)

        # Fuzzy lookup — CPU-bound; run in thread pool to keep event loop free
        fuzzy_distances: Dict[str, int] = {}
        if fuzzy:
            # Snapshot vocabulary for thread safety (dict.keys() is O(1) view copy)
            vocab_snapshot = list(self._inverted.keys())
            # Take a snapshot of the inverted index (shallow copy of dict is O(n),
            # but necessary to avoid race with concurrent writes in the thread)
            inverted_snapshot = {k: set(v) for k, v in self._inverted.items()}

            loop = asyncio.get_event_loop()
            fuzzy_distances = await loop.run_in_executor(
                _executor,
                bulk_fuzzy_resolve,
                tokens,
                inverted_snapshot,
                max_fuzzy_distance,
            )

        # Merge all candidates
        all_candidates = exact_candidates | prefix_candidates | set(fuzzy_distances.keys())

        # Entity type filter
        if entity_type:
            all_candidates = {
                cid for cid in all_candidates
                if self._docs.get(cid, {}).get("type") == entity_type
            }

        if not all_candidates:
            return []

        # ── Phase 2: Scoring ──────────────────────────────────────────

        # TF-IDF cosine scoring (threading.RLock handles concurrent reads internally)
        scored: List[Tuple[str, float]] = []
        for doc_id in all_candidates:
            if doc_id not in self._docs:
                continue

            # Base relevance: TF-IDF cosine similarity
            tfidf = self._tfidf.score(doc_id, tokens)

            # Structural bonus signals
            exact_bonus  = 0.25 if doc_id in exact_candidates else 0.0
            prefix_bonus = 0.15 if doc_id in prefix_candidates and doc_id not in exact_candidates else 0.0

            # Fuzzy penalty: more edits → lower score
            fuzzy_penalty = 0.0
            if doc_id in fuzzy_distances and doc_id not in exact_candidates and doc_id not in prefix_candidates:
                fuzzy_penalty = fuzzy_distances[doc_id] * 0.10

            final = tfidf + exact_bonus + prefix_bonus - fuzzy_penalty
            scored.append((doc_id, final))

        scored.sort(key=lambda x: x[1], reverse=True)

        # ── Phase 3: Result construction ─────────────────────────────

        results = []
        for doc_id, score in scored[:top_k]:
            doc = self._docs.get(doc_id)
            if doc is None:
                continue
            results.append({
                **doc,
                "score": round(score, 6),
                "tfidfScore": round(self._tfidf.score(doc_id, tokens), 6),
                "matchType": self._classify_match(doc_id, exact_candidates, prefix_candidates, fuzzy_distances),
            })

        return results

    def _classify_match(
        self,
        doc_id: str,
        exact: Set[str],
        prefix: Set[str],
        fuzzy: Dict[str, int],
    ) -> str:
        if doc_id in exact:
            return "exact"
        if doc_id in prefix:
            return "prefix"
        d = fuzzy.get(doc_id, "?")
        return f"fuzzy(editDistance={d})"

    # ------------------------------------------------------------------
    # Autocomplete
    # ------------------------------------------------------------------

    def suggest(self, prefix: str, max_results: int = 10) -> List[str]:
        """
        Return word suggestions via Trie DFS.
        O(k + m) where k = prefix length, m = matching words.
        """
        return self._trie.autocomplete(prefix.lower(), max_suggestions=max_results)

    # ------------------------------------------------------------------
    # Explain / introspection
    # ------------------------------------------------------------------

    def explain_query(self, query: str) -> dict:
        """
        Return per-token IDF scores and matching doc counts.
        Useful for understanding why certain documents rank higher.
        """
        tokens = _tokenize(query)
        token_info = []
        for token in tokens:
            token_info.append({
                "token": token,
                "idf": round(self._tfidf.idf(token), 4),
                "documentFrequency": len(self._inverted.get(token, set())),
                "prefixMatches": len(self._trie.search_prefix(token)),
            })
        return {
            "query": query,
            "tokens": token_info,
            "totalDocuments": len(self._docs),
            "vocabularySize": self._tfidf.vocabulary_size,
            "topDiscriminativeTerms": self._tfidf.top_terms(10),
        }

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        return {
            "totalDocuments": len(self._docs),
            "totalIndexed": self._stats_indexed,
            "totalRemoved": self._stats_removed,
            "vocabularySize": self._tfidf.vocabulary_size,
            "uniqueTokensInInvertedIndex": len(self._inverted),
        }
