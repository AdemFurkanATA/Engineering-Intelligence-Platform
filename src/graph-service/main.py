"""
Graph Service — FastAPI application.

Responsibilities (phases.md §Graph Service — the heart of the platform):
- Build the Living Knowledge Graph
- Create Repository, Document nodes
- Establish relationships (DESCRIBES, BELONGS_TO, etc.)
- Listen to domain events and update the graph accordingly
- Publish GraphUpdated events

For MVP, the graph is stored in-memory.
Swap `_nodes` and `_relationships` with a neo4j driver without changing
the public interface.
"""
import logging
import os
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from shared.kafka import EventPublisher, EventSubscriber
from shared.models import GraphUpdatedPayload, create_event

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory graph store
# ---------------------------------------------------------------------------
_nodes: Dict[str, dict] = {}          # node_id → node record
_relationships: List[dict] = []       # list of relationship records

publisher = EventPublisher()
subscriber = EventSubscriber(
    group_id="graph-service-group",
    topics=["repository.created", "repository.updated", "repository.deleted", "document.processed"],
)


# ---------------------------------------------------------------------------
# Graph helpers
# ---------------------------------------------------------------------------

def _upsert_node(node_id: str, label: str, properties: dict) -> dict:
    node = {
        "nodeId": node_id,
        "label": label,
        "properties": properties,
        "createdAt": _nodes.get(node_id, {}).get("createdAt", datetime.utcnow().isoformat() + "Z"),
        "updatedAt": datetime.utcnow().isoformat() + "Z",
    }
    _nodes[node_id] = node
    return node


def _add_relationship(from_id: str, to_id: str, rel_type: str, properties: Optional[dict] = None) -> dict:
    # Deduplicate by (from, to, type)
    for rel in _relationships:
        if rel["from"] == from_id and rel["to"] == to_id and rel["type"] == rel_type:
            return rel
    rel = {
        "relationshipId": str(uuid.uuid4()),
        "from": from_id,
        "to": to_id,
        "type": rel_type,
        "properties": properties or {},
        "createdAt": datetime.utcnow().isoformat() + "Z",
    }
    _relationships.append(rel)
    return rel


# ---------------------------------------------------------------------------
# Event handler
# ---------------------------------------------------------------------------

async def handle_event(topic: str, value: dict) -> None:
    event_type = value.get("eventType", "unknown")
    logger.info("Received event: type=%s id=%s", event_type, value.get("eventId"))

    org_id = value.get("organizationId", "")
    payload = value.get("payload", {})
    nodes_created = 0
    rels_created = 0

    if event_type == "RepositoryCreated":
        repo_id = payload.get("repositoryId", "")
        _upsert_node(repo_id, "Repository", {
            "name": payload.get("name"),
            "url": payload.get("url"),
            "language": payload.get("language"),
            "visibility": payload.get("visibility"),
            "defaultBranch": payload.get("defaultBranch"),
            "organizationId": org_id,
        })
        nodes_created = 1
        logger.info("Graph: created Repository node %s", repo_id)

    elif event_type == "RepositoryUpdated":
        repo_id = payload.get("repositoryId", "")
        if repo_id in _nodes:
            for change in payload.get("changes", []):
                _nodes[repo_id]["properties"][change] = payload.get(change)
            _nodes[repo_id]["updatedAt"] = datetime.utcnow().isoformat() + "Z"
            logger.info("Graph: updated Repository node %s", repo_id)

    elif event_type == "RepositoryDeleted":
        repo_id = payload.get("repositoryId", "")
        _nodes.pop(repo_id, None)
        global _relationships
        _relationships = [r for r in _relationships if r["from"] != repo_id and r["to"] != repo_id]
        logger.info("Graph: deleted Repository node %s and its relationships", repo_id)
        return  # No GraphUpdated event for deletion

    elif event_type == "DocumentProcessed":
        doc_id = payload.get("documentId", "")
        repo_id = payload.get("repositoryId", "")
        _upsert_node(doc_id, "Document", {
            "fileName": payload.get("fileName"),
            "documentType": payload.get("documentType"),
            "chunkCount": payload.get("chunkCount"),
            "wordCount": payload.get("wordCount"),
            "repositoryId": repo_id,
        })
        nodes_created = 1

        if repo_id in _nodes:
            rel = _add_relationship(doc_id, repo_id, "DESCRIBES")
            rels_created = 1 if rel else 0
            logger.info("Graph: Document(%s) -[DESCRIBES]-> Repository(%s)", doc_id, repo_id)

    else:
        return

    # Publish GraphUpdated event
    out_payload = GraphUpdatedPayload(
        triggerEvent=event_type,
        nodesCreated=nodes_created,
        relationshipsCreated=rels_created,
    )
    event = create_event(
        event_type="GraphUpdated",
        aggregate_id=str(uuid.uuid4()),
        organization_id=org_id,
        payload=out_payload,
    )
    await publisher.publish("graph.updated", event)


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
    title="Graph Service",
    description="Builds and maintains the Living Knowledge Graph.",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health", tags=["Operations"])
def health_check():
    return {"status": "ok", "service": "graph-service"}


@app.get("/graph/nodes", tags=["Graph"])
def list_nodes(label: Optional[str] = Query(None)):
    nodes = list(_nodes.values())
    if label:
        nodes = [n for n in nodes if n["label"] == label]
    return {"data": nodes, "total": len(nodes)}


@app.get("/graph/nodes/{node_id}", tags=["Graph"])
def get_node(node_id: str):
    node = _nodes.get(node_id)
    if node is None:
        raise HTTPException(status_code=404, detail=f"Node '{node_id}' not found.")
    return {"data": node}


@app.get("/graph/relationships", tags=["Graph"])
def list_relationships(
    from_id: Optional[str] = Query(None, alias="fromId"),
    to_id: Optional[str] = Query(None, alias="toId"),
    rel_type: Optional[str] = Query(None, alias="type"),
):
    rels = _relationships[:]
    if from_id:
        rels = [r for r in rels if r["from"] == from_id]
    if to_id:
        rels = [r for r in rels if r["to"] == to_id]
    if rel_type:
        rels = [r for r in rels if r["type"] == rel_type]
    return {"data": rels, "total": len(rels)}


@app.get("/graph/stats", tags=["Graph"])
def graph_stats():
    labels = {}
    for node in _nodes.values():
        labels[node["label"]] = labels.get(node["label"], 0) + 1
    rel_types = {}
    for rel in _relationships:
        rel_types[rel["type"]] = rel_types.get(rel["type"], 0) + 1
    return {
        "totalNodes": len(_nodes),
        "totalRelationships": len(_relationships),
        "nodesByLabel": labels,
        "relationshipsByType": rel_types,
    }
