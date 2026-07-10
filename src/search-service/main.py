"""
Search Service — FastAPI application.

Responsibilities (phases.md §Search Service):
- Hybrid search combining:
  - BM25 keyword search (implemented)
  - Semantic/vector search (stub — real cosine similarity with embeddings)
  - Graph traversal hints (stub)
- Listen to events to keep the search index updated
- Return documents, repositories, and related components

For MVP, all indexing and search is in-memory using a simple TF-IDF-like
scoring (BM25 approximation). Replace with OpenSearch or Elasticsearch
for production without changing the public API.
"""
import logging
import math
import os
import re
import sys
from collections import Counter
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from shared.kafka import EventSubscriber
from shared.models import SearchIndexedPayload, create_event
from shared.kafka import EventPublisher

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory search index
# ---------------------------------------------------------------------------

# Each entry: {id, type, text, metadata, indexedAt}
_index: Dict[str, dict] = {}

# Inverted index: token → set of doc ids
_inverted: Dict[str, set] = {}

publisher = EventPublisher()
subscriber = EventSubscriber(
    group_id="search-service-group",
    topics=["repository.created", "repository.updated", "repository.deleted", "document.processed"],
)


# ---------------------------------------------------------------------------
# Indexing helpers
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> List[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def _index_entry(entry_id: str, entry_type: str, text: str, metadata: dict) -> None:
    _index[entry_id] = {
        "id": entry_id,
        "type": entry_type,
        "text": text,
        "metadata": metadata,
        "indexedAt": datetime.utcnow().isoformat() + "Z",
    }
    tokens = set(_tokenize(text))
    for token in tokens:
        _inverted.setdefault(token, set()).add(entry_id)


def _remove_from_index(entry_id: str) -> None:
    entry = _index.pop(entry_id, None)
    if entry:
        for token_set in _inverted.values():
            token_set.discard(entry_id)


# ---------------------------------------------------------------------------
# BM25-style scoring
# ---------------------------------------------------------------------------

K1 = 1.5
B = 0.75


def _bm25_score(query_tokens: List[str], entry_id: str) -> float:
    entry = _index.get(entry_id)
    if not entry:
        return 0.0
    doc_tokens = _tokenize(entry["text"])
    doc_len = len(doc_tokens)
    avg_len = sum(len(_tokenize(e["text"])) for e in _index.values()) / max(len(_index), 1)
    tf_map = Counter(doc_tokens)
    N = len(_index)
    score = 0.0
    for token in query_tokens:
        df = len(_inverted.get(token, set()))
        if df == 0:
            continue
        idf = math.log((N - df + 0.5) / (df + 0.5) + 1)
        tf = tf_map.get(token, 0)
        tf_norm = (tf * (K1 + 1)) / (tf + K1 * (1 - B + B * doc_len / max(avg_len, 1)))
        score += idf * tf_norm
    return score


def _search(query: str, top_k: int = 10, entity_type: Optional[str] = None) -> List[dict]:
    if not query.strip():
        return []
    tokens = _tokenize(query)
    candidate_ids: set = set()
    for token in tokens:
        candidate_ids |= _inverted.get(token, set())

    if entity_type:
        candidate_ids = {cid for cid in candidate_ids if _index.get(cid, {}).get("type") == entity_type}

    scored = [(cid, _bm25_score(tokens, cid)) for cid in candidate_ids]
    scored.sort(key=lambda x: x[1], reverse=True)

    results = []
    for cid, score in scored[:top_k]:
        entry = _index[cid].copy()
        entry["score"] = round(score, 4)
        results.append(entry)
    return results


# ---------------------------------------------------------------------------
# Event handler
# ---------------------------------------------------------------------------

async def handle_event(topic: str, value: dict) -> None:
    event_type = value.get("eventType", "unknown")
    logger.info("Received event: type=%s id=%s", event_type, value.get("eventId"))

    payload = value.get("payload", {})

    if event_type == "RepositoryCreated":
        repo_id = payload.get("repositoryId", "")
        text = " ".join(filter(None, [
            payload.get("name", ""),
            payload.get("language", ""),
            payload.get("url", ""),
        ]))
        _index_entry(repo_id, "Repository", text, payload)
        logger.info("Search: indexed Repository %s", repo_id)

    elif event_type == "RepositoryUpdated":
        repo_id = payload.get("repositoryId", "")
        if repo_id in _index:
            existing = _index[repo_id]
            _index_entry(repo_id, "Repository", existing["text"], existing["metadata"])

    elif event_type == "RepositoryDeleted":
        repo_id = payload.get("repositoryId", "")
        _remove_from_index(repo_id)
        logger.info("Search: removed Repository %s from index", repo_id)

    elif event_type == "DocumentProcessed":
        doc_id = payload.get("documentId", "")
        text = " ".join(filter(None, [
            payload.get("fileName", ""),
            payload.get("documentType", ""),
        ]))
        _index_entry(doc_id, "Document", text, payload)
        logger.info("Search: indexed Document %s", doc_id)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    await publisher.start()
    await subscriber.start(handle_event)
    yield
    await subscriber.stop()
    await publisher.stop()


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Search Service",
    description="Provides hybrid full-text and semantic search across the knowledge base.",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health", tags=["Operations"])
def health_check():
    return {"status": "ok", "service": "search-service"}


@app.get("/search", tags=["Search"])
def hybrid_search(
    q: str = Query(..., description="Search query string"),
    top_k: int = Query(10, alias="topK", ge=1, le=100),
    entity_type: Optional[str] = Query(None, alias="type", description="Filter by entity type: Repository | Document"),
):
    """
    Hybrid search endpoint.
    Combines BM25 keyword scoring (implemented) with semantic search (stub).
    Returns matched entities with relevance scores.
    """
    results = _search(q, top_k=top_k, entity_type=entity_type)
    return {
        "query": q,
        "results": results,
        "total": len(results),
        "searchType": "bm25",
    }


@app.get("/search/index/stats", tags=["Search"])
def index_stats():
    types = Counter(e["type"] for e in _index.values())
    return {
        "totalIndexed": len(_index),
        "uniqueTokens": len(_inverted),
        "byType": dict(types),
    }
