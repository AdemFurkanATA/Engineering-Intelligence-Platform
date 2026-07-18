"""
Embedding Service — FastAPI application.

Responsibilities (phases.md §Embedding Service):
- Listen to DocumentProcessed events
- Generate real vector embeddings using sentence-transformers
- Store vectors in Qdrant vector database
- Version embeddings
- Publish EmbeddingGenerated events

Storage strategy
----------------
1. Model:  sentence-transformers/all-MiniLM-L6-v2 (384-dim, free, local)
           Falls back to deterministic hash stub if model load fails.
2. Store:  Qdrant collection "engineering-documents"
           Falls back to in-memory dict if Qdrant is unavailable.

Model is loaded once at startup and reused for all requests.
"""
import hashlib
import logging
import math
import os
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from shared.kafka import EventPublisher, EventSubscriber
from shared.models import EmbeddingGeneratedPayload, create_event
from shared.config import QDRANT_URL

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
COLLECTION_NAME = "engineering-documents"
VECTOR_DIM      = 384          # all-MiniLM-L6-v2 output dimension
MODEL_NAME      = "all-MiniLM-L6-v2"
STUB_MODEL_NAME = "stub-embedder-v1"
STUB_DIM        = 128

# ---------------------------------------------------------------------------
# Backend state
# ---------------------------------------------------------------------------
_model       = None            # SentenceTransformer instance or None
_qdrant      = None            # QdrantClient instance or None
_embeddings: Dict[str, dict] = {}   # in-memory fallback: doc_id → metadata

publisher = EventPublisher()
subscriber = EventSubscriber(
    group_id="embedding-service-group",
    topics=["document.processed"],
)


# ---------------------------------------------------------------------------
# Backend initialisation
# ---------------------------------------------------------------------------

async def _init_model() -> None:
    """Load sentence-transformers model. Falls back gracefully on failure."""
    global _model
    try:
        from sentence_transformers import SentenceTransformer
        logger.info("Loading embedding model '%s' — this may take a moment on first run…", MODEL_NAME)
        _model = SentenceTransformer(MODEL_NAME)
        logger.info("Embedding model loaded: %s (dim=%d)", MODEL_NAME, VECTOR_DIM)
    except Exception as exc:
        logger.warning("Could not load sentence-transformers model (%s). Using hash stub.", exc)
        _model = None


async def _init_qdrant() -> None:
    """Connect to Qdrant and ensure the collection exists."""
    global _qdrant
    try:
        from qdrant_client import QdrantClient
        from qdrant_client.models import Distance, VectorParams

        client = QdrantClient(url=QDRANT_URL, timeout=10)
        # Verify connectivity
        client.get_collections()
        _qdrant = client

        # Create collection if it doesn't exist
        existing = {c.name for c in _qdrant.get_collections().collections}
        if COLLECTION_NAME not in existing:
            _qdrant.create_collection(
                collection_name=COLLECTION_NAME,
                vectors_config=VectorParams(size=VECTOR_DIM, distance=Distance.COSINE),
            )
            logger.info("Qdrant collection '%s' created (dim=%d, cosine).", COLLECTION_NAME, VECTOR_DIM)
        else:
            logger.info("Qdrant collection '%s' already exists.", COLLECTION_NAME)

        logger.info("Qdrant connected: %s", QDRANT_URL)
    except Exception as exc:
        logger.warning("Qdrant unavailable (%s). Using in-memory vector store.", exc)
        _qdrant = None


# ---------------------------------------------------------------------------
# Embedding generation
# ---------------------------------------------------------------------------

def _stub_embed(text: str) -> List[float]:
    """Deterministic hash-based stub when real model is unavailable."""
    digest = hashlib.sha256(text.encode()).digest()
    raw  = [(digest[i % 32] / 255.0) * 2 - 1 for i in range(STUB_DIM)]
    norm = math.sqrt(sum(v * v for v in raw)) or 1.0
    return [v / norm for v in raw]


def _embed_texts(texts: List[str]) -> List[List[float]]:
    """Generate embeddings for a list of texts using the loaded model or stub."""
    if _model is not None:
        vectors = _model.encode(texts, convert_to_numpy=True, show_progress_bar=False)
        return [v.tolist() for v in vectors]
    return [_stub_embed(t) for t in texts]


def _active_model_name() -> str:
    return MODEL_NAME if _model is not None else STUB_MODEL_NAME


def _active_dim() -> int:
    return VECTOR_DIM if _model is not None else STUB_DIM


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Vector storage
# ---------------------------------------------------------------------------

def _store_in_qdrant(doc_id: str, repo_id: str, org_id: str, chunk_texts: List[str], vectors: List[List[float]]) -> None:
    """Upsert chunk vectors into Qdrant."""
    from qdrant_client.models import PointStruct

    points = []
    for i, (text, vector) in enumerate(zip(chunk_texts, vectors)):
        point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{doc_id}:chunk:{i}"))
        points.append(PointStruct(
            id=point_id,
            vector=vector,
            payload={
                "document_id":   doc_id,
                "repository_id": repo_id,
                "organization_id": org_id,
                "chunk_index":   i,
                "text_preview":  text[:200],   # store first 200 chars for retrieval preview
            },
        ))
    _qdrant.upsert(collection_name=COLLECTION_NAME, points=points, wait=True)
    logger.info("Qdrant: upserted %d vectors for doc=%s", len(points), doc_id)


def _store_in_memory(doc_id: str, repo_id: str, org_id: str, vectors: List[List[float]]) -> None:
    _embeddings[doc_id] = {
        "documentId":    doc_id,
        "repositoryId":  repo_id,
        "organizationId": org_id,
        "modelName":     _active_model_name(),
        "vectorDim":     _active_dim(),
        "chunkCount":    len(vectors),
        "generatedAt":   _now_iso(),
        "vectors":       vectors,
    }


# ---------------------------------------------------------------------------
# Event handler
# ---------------------------------------------------------------------------

async def handle_event(topic: str, value: dict) -> None:
    event_type = value.get("eventType", "unknown")
    logger.info("Received event: type=%s id=%s", event_type, value.get("eventId"))

    if event_type != "DocumentProcessed":
        return

    payload     = value.get("payload", {})
    doc_id      = payload.get("documentId", "")
    repo_id     = payload.get("repositoryId", "")
    org_id      = value.get("organizationId", "")
    chunk_count = max(payload.get("chunkCount", 1), 1)
    text_preview = payload.get("textPreview", "")

    # Use real text from the event payload when available.
    # text_preview carries the first ~500 words of actual document content.
    # If absent (legacy event), fall back to identifier-based placeholder.
    if text_preview:
        words = text_preview.split()
        chunk_size = max(len(words) // chunk_count, 1)
        chunk_texts = [
            " ".join(words[i * chunk_size:(i + 1) * chunk_size])
            for i in range(chunk_count)
        ]
        # Ensure last chunk captures any remaining words
        if len(words) > chunk_count * chunk_size:
            chunk_texts[-1] += " " + " ".join(words[chunk_count * chunk_size:])
    else:
        # Fallback: placeholder text (no real content in event)
        logger.warning(
            "No textPreview in DocumentProcessed event for doc=%s — using placeholder embedding", doc_id
        )
        chunk_texts = [f"{doc_id}:chunk:{i}" for i in range(chunk_count)]

    vectors = _embed_texts(chunk_texts)

    if _qdrant is not None:
        _store_in_qdrant(doc_id, repo_id, org_id, chunk_texts, vectors)
    else:
        _store_in_memory(doc_id, repo_id, org_id, vectors)

    # Build and store metadata record regardless of backend
    _embeddings[doc_id] = {
        "documentId":    doc_id,
        "repositoryId":  repo_id,
        "organizationId": org_id,
        "modelName":     _active_model_name(),
        "vectorDim":     _active_dim(),
        "chunkCount":    len(vectors),
        "generatedAt":   _now_iso(),
        "backend":       "qdrant" if _qdrant else "in-memory",
    }

    out_payload = EmbeddingGeneratedPayload(
        documentId=doc_id,
        repositoryId=repo_id,
        chunksProcessed=len(vectors),
        modelName=_active_model_name(),
    )
    event = create_event(
        event_type="EmbeddingGenerated",
        aggregate_id=doc_id,
        organization_id=org_id,
        payload=out_payload,
    )
    await publisher.publish("embedding.generated", event)
    logger.info(
        "EmbeddingGenerated: doc_id=%s chunks=%d model=%s backend=%s",
        doc_id, len(vectors), _active_model_name(), "qdrant" if _qdrant else "in-memory",
    )


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    await _init_model()
    await _init_qdrant()
    await publisher.start()
    await subscriber.start(handle_event)
    yield
    await subscriber.stop()
    await publisher.stop()
    if _qdrant is not None:
        _qdrant.close()


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Embedding Service",
    description=(
        "Generates real vector embeddings using sentence-transformers/all-MiniLM-L6-v2 "
        "and stores them in Qdrant. Falls back to hash stub + in-memory if unavailable."
    ),
    version="2.0.0",
    lifespan=lifespan,
)


@app.get("/health", tags=["Operations"])
def health_check():
    return {
        "status":    "ok",
        "service":   "embedding-service",
        "model":     _active_model_name(),
        "vectorDim": _active_dim(),
        "backend":   "qdrant" if _qdrant else "in-memory",
    }


@app.get("/embeddings", tags=["Embeddings"])
def list_embeddings(repository_id: Optional[str] = Query(None, alias="repositoryId")):
    records = list(_embeddings.values())
    if repository_id:
        records = [r for r in records if r["repositoryId"] == repository_id]
    # Omit raw vectors from list response (they can be large)
    summary = [{k: v for k, v in r.items() if k != "vectors"} for r in records]
    return {"data": summary, "total": len(summary)}


@app.get("/embeddings/{document_id}", tags=["Embeddings"])
def get_embedding(
    document_id: str,
    include_vectors: bool = Query(False, alias="includeVectors"),
):
    record = _embeddings.get(document_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"No embeddings found for document '{document_id}'.")
    if not include_vectors:
        record = {k: v for k, v in record.items() if k != "vectors"}
    return {"data": record}


@app.get("/embeddings/search/similar", tags=["Embeddings"])
def search_similar(
    q: str = Query(..., description="Query text to find similar documents"),
    top_k: int = Query(5, alias="topK", ge=1, le=50),
):
    """
    Find documents semantically similar to the query text using Qdrant.
    Only available when Qdrant backend is active.
    """
    if _qdrant is None:
        raise HTTPException(
            status_code=503,
            detail="Semantic similarity search requires Qdrant. Service is running in in-memory mode."
        )
    query_vector = _embed_texts([q])[0]
    results = _qdrant.search(
        collection_name=COLLECTION_NAME,
        query_vector=query_vector,
        limit=top_k,
    )
    return {
        "query": q,
        "results": [
            {
                "score":      r.score,
                "documentId": r.payload.get("document_id"),
                "chunkIndex": r.payload.get("chunk_index"),
                "preview":    r.payload.get("text_preview"),
            }
            for r in results
        ],
        "total": len(results),
    }
