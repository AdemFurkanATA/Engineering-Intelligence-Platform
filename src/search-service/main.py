"""
Search Service — FastAPI application.

Public API:
  GET  /health                    — health check
  GET  /search                    — hybrid search (exact + prefix + fuzzy + TF-IDF)
  GET  /search/suggest            — autocomplete via Trie
  GET  /search/explain            — query introspection (IDF per token, doc freq)
  GET  /search/stats              — index statistics
  GET  /search/similar/{id}       — find documents similar to a known document

The search engine (engine.py) provides:
  - Trie-based prefix search
  - Levenshtein fuzzy matching
  - TF-IDF cosine similarity ranking
  - asyncio.Lock for coroutine-level thread safety
  - threading.RLock inside TFIDFIndex for executor thread safety
  - ThreadPoolExecutor offloading for CPU-bound fuzzy resolution
"""
import logging
import os
import sys
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Query

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine import SearchEngine
from shared.kafka import EventSubscriber
from shared.kafka import EventPublisher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Singleton search engine — shared across all requests
# ---------------------------------------------------------------------------
engine = SearchEngine()

# ---------------------------------------------------------------------------
# Kafka
# ---------------------------------------------------------------------------
publisher = EventPublisher()
subscriber = EventSubscriber(
    group_id="search-service-group",
    topics=[
        "repository.created",
        "repository.updated",
        "repository.deleted",
        "document.processed",
    ],
)


async def handle_event(topic: str, value: dict) -> None:
    event_type = value.get("eventType", "unknown")
    logger.info("Event received: %s (topic=%s)", event_type, topic)
    payload = value.get("payload", {})

    if event_type == "RepositoryCreated":
        repo_id = payload.get("repositoryId", "")
        text = " ".join(filter(None, [
            payload.get("name", ""),
            payload.get("language", ""),
            payload.get("url", ""),
        ]))
        await engine.add(repo_id, "Repository", text, payload)
        logger.info("Search: indexed Repository %s", repo_id)

    elif event_type == "RepositoryUpdated":
        repo_id = payload.get("repositoryId", "")
        # Re-index with updated data
        text = " ".join(filter(None, [
            payload.get("name", ""),
            payload.get("language", ""),
            payload.get("url", ""),
        ]))
        if text.strip():
            await engine.add(repo_id, "Repository", text, payload)

    elif event_type == "RepositoryDeleted":
        repo_id = payload.get("repositoryId", "")
        removed = await engine.remove(repo_id)
        if removed:
            logger.info("Search: de-indexed Repository %s", repo_id)

    elif event_type == "DocumentProcessed":
        doc_id = payload.get("documentId", "")
        text = " ".join(filter(None, [
            payload.get("fileName", ""),
            payload.get("documentType", ""),
            payload.get("repositoryId", ""),
        ]))
        await engine.add(doc_id, "Document", text, payload)
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
    description=(
        "Hybrid search over the Engineering Intelligence Platform knowledge base. "
        "Combines Trie prefix search, Levenshtein fuzzy matching, and TF-IDF "
        "cosine similarity ranking."
    ),
    version="2.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health", tags=["Operations"])
def health_check():
    return {"status": "ok", "service": "search-service", "version": "2.0.0"}


@app.get("/search", tags=["Search"])
async def hybrid_search(
    q: str = Query(..., min_length=1, description="Search query"),
    top_k: int = Query(10, alias="topK", ge=1, le=100),
    entity_type: Optional[str] = Query(
        None, alias="type", description="Filter: Repository | Document"
    ),
    fuzzy: bool = Query(True, description="Enable fuzzy (typo-tolerant) matching"),
    max_fuzzy_distance: int = Query(
        2, alias="maxFuzzyDistance", ge=0, le=4,
        description="Maximum Levenshtein edit distance for fuzzy matching"
    ),
):
    """
    Hybrid search endpoint.

    Matching strategies (applied in order, results merged and ranked):
    - **Exact**: token found in inverted index verbatim
    - **Prefix**: token is a prefix of an indexed token (Trie)
    - **Fuzzy**: token is within `maxFuzzyDistance` Levenshtein edits of an indexed token

    Ranking:
    - **TF-IDF cosine similarity** — base relevance score
    - **Exact match bonus** (+0.25)
    - **Prefix match bonus** (+0.15)
    - **Fuzzy penalty** (−0.10 per edit)

    Response includes `matchType` per result for full transparency.
    """
    results = await engine.search(
        query=q,
        top_k=top_k,
        entity_type=entity_type,
        fuzzy=fuzzy,
        max_fuzzy_distance=max_fuzzy_distance,
    )
    return {
        "query": q,
        "results": results,
        "total": len(results),
        "config": {
            "fuzzyEnabled": fuzzy,
            "maxFuzzyDistance": max_fuzzy_distance,
            "topK": top_k,
        },
    }


@app.get("/search/suggest", tags=["Search"])
def autocomplete(
    q: str = Query(..., min_length=1, description="Prefix to complete"),
    limit: int = Query(10, ge=1, le=50),
):
    """
    Autocomplete suggestions using the Trie prefix tree.
    Returns up to `limit` words starting with the given prefix.
    Time complexity: O(k + m) where k = prefix length, m = matched words.
    """
    suggestions = engine.suggest(q, max_results=limit)
    return {"prefix": q, "suggestions": suggestions, "total": len(suggestions)}


@app.get("/search/explain", tags=["Search"])
def explain_query(
    q: str = Query(..., min_length=1, description="Query to explain"),
):
    """
    Introspect a query: shows IDF score, document frequency, and prefix match count
    for each token. Useful for understanding why documents rank the way they do.
    """
    return engine.explain_query(q)


@app.get("/search/stats", tags=["Search"])
def index_stats():
    """Return search index statistics."""
    return engine.stats()


@app.get("/search/similar/{document_id}", tags=["Search"])
async def find_similar(
    document_id: str,
    top_k: int = Query(5, alias="topK", ge=1, le=20),
):
    """
    Find documents similar to a known indexed document.
    Uses the document's own text as the search query.
    """
    stats = engine.stats()
    if stats["totalDocuments"] == 0:
        raise HTTPException(status_code=404, detail="Index is empty.")

    # Access internal docs for the source text
    source = engine._docs.get(document_id)
    if source is None:
        raise HTTPException(status_code=404, detail=f"Document '{document_id}' not in index.")

    results = await engine.search(
        query=source["text"],
        top_k=top_k + 1,  # +1 because the source itself will match
        fuzzy=False,       # similarity search: exact + prefix only
    )

    # Exclude the source document from results
    results = [r for r in results if r["id"] != document_id][:top_k]
    return {
        "sourceId": document_id,
        "sourceType": source["type"],
        "similar": results,
        "total": len(results),
    }


# ---------------------------------------------------------------------------
# Admin: manually index a document (useful for testing without Kafka)
# ---------------------------------------------------------------------------

from pydantic import BaseModel


class IndexRequest(BaseModel):
    id: str
    type: str
    text: str
    metadata: dict = {}


@app.post("/search/index", status_code=201, tags=["Admin"])
async def manually_index(req: IndexRequest):
    """
    Manually add a document to the search index (for testing/development).
    In production, documents arrive via Kafka events.
    """
    await engine.add(req.id, req.type, req.text, req.metadata)
    return {"indexed": True, "id": req.id, "stats": engine.stats()}


@app.delete("/search/index/{document_id}", tags=["Admin"])
async def manually_remove(document_id: str):
    """Manually remove a document from the search index."""
    removed = await engine.remove(document_id)
    if not removed:
        raise HTTPException(status_code=404, detail=f"'{document_id}' not in index.")
    return {"removed": True, "id": document_id}
