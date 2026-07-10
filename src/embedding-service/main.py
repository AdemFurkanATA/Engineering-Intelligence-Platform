"""
Embedding Service — FastAPI application.

Responsibilities (phases.md §Embedding Service):
- Listen to DocumentProcessed events
- Generate vector embeddings for each chunk
- Store vectors (in-memory for MVP; Qdrant in production)
- Version embeddings
- Publish EmbeddingGenerated events

For MVP, embeddings are deterministic stubs (hash-based float vectors).
Replace _embed() with a real model (sentence-transformers, OpenAI, etc.)
without changing any other code.
"""
import hashlib
import logging
import math
import os
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from shared.kafka import EventPublisher, EventSubscriber
from shared.models import EmbeddingGeneratedPayload, create_event

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory vector store (swap for Qdrant client in production)
# ---------------------------------------------------------------------------
_embeddings: Dict[str, dict] = {}   # doc_id → embedding record

VECTOR_DIM = 128
MODEL_NAME = "stub-embedder-v1"

publisher = EventPublisher()
subscriber = EventSubscriber(
    group_id="embedding-service-group",
    topics=["document.processed"],
)


# ---------------------------------------------------------------------------
# Stub embedder
# ---------------------------------------------------------------------------

def _embed(text: str) -> List[float]:
    """
    Deterministic stub embedder.
    Produces a consistent VECTOR_DIM-length unit vector for any input text.
    Replace this function with a real model for production.
    """
    digest = hashlib.sha256(text.encode()).digest()
    raw = [
        (digest[i % 32] / 255.0) * 2 - 1
        for i in range(VECTOR_DIM)
    ]
    norm = math.sqrt(sum(v * v for v in raw)) or 1.0
    return [v / norm for v in raw]


# ---------------------------------------------------------------------------
# Event handler
# ---------------------------------------------------------------------------

async def handle_event(topic: str, value: dict) -> None:
    event_type = value.get("eventType", "unknown")
    logger.info("Received event: type=%s id=%s", event_type, value.get("eventId"))

    if event_type != "DocumentProcessed":
        return

    payload = value.get("payload", {})
    doc_id = payload.get("documentId", "")
    repo_id = payload.get("repositoryId", "")
    org_id = value.get("organizationId", "")
    chunk_count = payload.get("chunkCount", 1)

    # Generate one embedding vector per chunk (stub uses doc_id + chunk index)
    chunk_vectors = [
        _embed(f"{doc_id}:chunk:{i}") for i in range(max(chunk_count, 1))
    ]

    _embeddings[doc_id] = {
        "documentId": doc_id,
        "repositoryId": repo_id,
        "organizationId": org_id,
        "modelName": MODEL_NAME,
        "vectorDim": VECTOR_DIM,
        "chunkCount": len(chunk_vectors),
        "generatedAt": datetime.utcnow().isoformat() + "Z",
        "vectors": chunk_vectors,
    }

    out_payload = EmbeddingGeneratedPayload(
        documentId=doc_id,
        repositoryId=repo_id,
        chunksProcessed=len(chunk_vectors),
        modelName=MODEL_NAME,
    )
    event = create_event(
        event_type="EmbeddingGenerated",
        aggregate_id=doc_id,
        organization_id=org_id,
        payload=out_payload,
    )
    await publisher.publish("embedding.generated", event)
    logger.info("EmbeddingGenerated: doc_id=%s chunks=%d", doc_id, len(chunk_vectors))


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
    title="Embedding Service",
    description="Generates and stores vector embeddings for engineering documents.",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health", tags=["Operations"])
def health_check():
    return {"status": "ok", "service": "embedding-service"}


@app.get("/embeddings", tags=["Embeddings"])
def list_embeddings(repository_id: Optional[str] = Query(None, alias="repositoryId")):
    records = list(_embeddings.values())
    if repository_id:
        records = [r for r in records if r["repositoryId"] == repository_id]
    # Return metadata only, not raw vectors (can be large)
    summary = [
        {k: v for k, v in r.items() if k != "vectors"}
        for r in records
    ]
    return {"data": summary, "total": len(summary)}


@app.get("/embeddings/{document_id}", tags=["Embeddings"])
def get_embedding(document_id: str, include_vectors: bool = Query(False, alias="includeVectors")):
    record = _embeddings.get(document_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"No embeddings found for document '{document_id}'.")
    if not include_vectors:
        record = {k: v for k, v in record.items() if k != "vectors"}
    return {"data": record}
