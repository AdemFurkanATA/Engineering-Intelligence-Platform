"""
Event Service — FastAPI application.

Responsibilities (phases.md §Event Service):
- Kafka topic catalog
- Event history (in-memory ring buffer for MVP)
- Event governance: list producers/consumers per topic

This service acts as the observability layer for the event-driven platform.
"""
import logging
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from typing import List, Optional

from fastapi import FastAPI, Query

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from shared.kafka import EventSubscriber

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Event catalog (aligned with EVENT_CATALOG.md)
# ---------------------------------------------------------------------------

TOPIC_CATALOG = [
    {
        "topic": "repository.created",
        "eventType": "RepositoryCreated",
        "producer": "repository-service",
        "consumers": ["document-service", "embedding-service", "graph-service", "search-service"],
        "description": "Published when a new repository is registered.",
    },
    {
        "topic": "repository.updated",
        "eventType": "RepositoryUpdated",
        "producer": "repository-service",
        "consumers": ["graph-service", "search-service"],
        "description": "Published when repository metadata is modified.",
    },
    {
        "topic": "repository.deleted",
        "eventType": "RepositoryDeleted",
        "producer": "repository-service",
        "consumers": ["document-service", "graph-service", "search-service"],
        "description": "Published when a repository is permanently removed.",
    },
    {
        "topic": "document.processed",
        "eventType": "DocumentProcessed",
        "producer": "document-service",
        "consumers": ["embedding-service", "graph-service", "search-service"],
        "description": "Published after a document is extracted and chunked.",
    },
    {
        "topic": "embedding.generated",
        "eventType": "EmbeddingGenerated",
        "producer": "embedding-service",
        "consumers": ["search-service"],
        "description": "Published after vector embeddings are generated for a document.",
    },
    {
        "topic": "graph.updated",
        "eventType": "GraphUpdated",
        "producer": "graph-service",
        "consumers": [],
        "description": "Published after the knowledge graph is updated.",
    },
]

# In-memory event log (last 1000 events, FIFO)
_MAX_LOG = 1000
_event_log: List[dict] = []

ALL_TOPICS = [t["topic"] for t in TOPIC_CATALOG]

subscriber = EventSubscriber(
    group_id="event-service-group",
    topics=ALL_TOPICS,
)


# ---------------------------------------------------------------------------
# Event handler — capture all events for observability
# ---------------------------------------------------------------------------

async def handle_event(topic: str, value: dict) -> None:
    entry = {
        "topic": topic,
        "eventId": value.get("eventId"),
        "eventType": value.get("eventType"),
        "aggregateId": value.get("aggregateId"),
        "organizationId": value.get("organizationId"),
        "timestamp": value.get("timestamp"),
        "receivedAt": datetime.utcnow().isoformat() + "Z",
    }
    _event_log.append(entry)
    if len(_event_log) > _MAX_LOG:
        _event_log.pop(0)
    logger.info("Event captured: type=%s topic=%s", entry["eventType"], topic)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    await subscriber.start(handle_event)
    yield
    await subscriber.stop()


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Event Service",
    description="Event catalog and observability layer for the platform.",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health", tags=["Operations"])
def health_check():
    return {"status": "ok", "service": "event-service"}


@app.get("/events/topics", tags=["Events"])
def list_topics():
    """Return the complete event topic catalog."""
    return {"data": TOPIC_CATALOG, "total": len(TOPIC_CATALOG)}


@app.get("/events/topics/{topic_name}", tags=["Events"])
def get_topic(topic_name: str):
    """Get catalog entry for a specific topic."""
    for entry in TOPIC_CATALOG:
        if entry["topic"] == topic_name:
            return {"data": entry}
    from fastapi import HTTPException
    raise HTTPException(status_code=404, detail=f"Topic '{topic_name}' not in catalog.")


@app.get("/events/log", tags=["Events"])
def get_event_log(
    event_type: Optional[str] = Query(None, alias="eventType"),
    topic: Optional[str] = None,
    limit: int = Query(50, ge=1, le=_MAX_LOG),
):
    """Return recent events captured from all topics (last 1000)."""
    log = _event_log[:]
    if event_type:
        log = [e for e in log if e.get("eventType") == event_type]
    if topic:
        log = [e for e in log if e.get("topic") == topic]
    # newest first
    log = list(reversed(log))[:limit]
    return {"data": log, "total": len(log)}
