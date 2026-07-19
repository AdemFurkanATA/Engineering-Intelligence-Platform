"""
Graph Service — FastAPI application.

Responsibilities (phases.md §Graph Service — the heart of the platform):
- Build the Living Knowledge Graph
- Create Repository, Document nodes
- Establish relationships (DESCRIBES, BELONGS_TO, etc.)
- Listen to domain events and update the graph accordingly
- Publish GraphUpdated events

Storage strategy
----------------
When Neo4j is available (_driver is not None), all operations use Cypher
queries via the async Neo4j driver.  MERGE semantics ensure idempotency.
If Neo4j is unavailable at startup the service falls back to in-memory
dictionaries with a WARNING log — identical to the original MVP behaviour.
"""
import logging
import os
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from shared.kafka import EventPublisher, EventSubscriber
from shared.models import GraphUpdatedPayload, create_event
from shared.config import NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Backend state
# ---------------------------------------------------------------------------
_driver = None                        # neo4j.AsyncDriver, None if unavailable
_nodes: Dict[str, dict] = {}         # in-memory fallback
_relationships: List[dict] = []      # in-memory fallback

publisher = EventPublisher()
subscriber = EventSubscriber(
    group_id="graph-service-group",
    topics=["repository.created", "repository.updated", "repository.deleted", "document.processed"],
)

# ---------------------------------------------------------------------------
# Security: Cypher label and relationship type whitelists
# Prevents injection via user-supplied label/type query parameters.
# ---------------------------------------------------------------------------
ALLOWED_LABELS: frozenset = frozenset({
    "Repository", "Document", "Developer", "Service",
    "Function", "Class", "API", "Database", "KafkaTopic",
})
ALLOWED_REL_TYPES: frozenset = frozenset({
    "DESCRIBES", "DEPENDS_ON", "CALLS", "IMPLEMENTS",
    "PRODUCES", "CONSUMES", "MODIFIES", "REFERENCES",
    "CREATED_BY", "RELATED_TO", "BELONGS_TO",
})


# ---------------------------------------------------------------------------
# Neo4j initialisation
# ---------------------------------------------------------------------------

async def _init_neo4j():
    """Try to connect to Neo4j and create required indexes."""
    global _driver
    try:
        from neo4j import AsyncGraphDatabase
        driver = AsyncGraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
        # Verify connectivity
        await driver.verify_connectivity()
        _driver = driver

        # Create indexes for fast lookups
        async with _driver.session() as session:
            for label in ("Repository", "Document", "Developer"):
                await session.run(
                    f"CREATE INDEX IF NOT EXISTS FOR (n:{label}) ON (n.nodeId)"
                )
        logger.info("Neo4j connected: %s", NEO4J_URI)
    except Exception as exc:
        logger.warning("Neo4j unavailable (%s). Using in-memory graph.", exc)
        _driver = None


# ---------------------------------------------------------------------------
# Graph helpers — dual-backend
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Neo4j path ──────────────────────────────────────────────────────────────

async def _neo4j_upsert_node(node_id: str, label: str, properties: dict) -> dict:
    props = {k: v for k, v in properties.items() if v is not None}
    props["nodeId"] = node_id
    props["updatedAt"] = _now_iso()

    async with _driver.session() as session:
        result = await session.run(
            f"""
            MERGE (n:{label} {{nodeId: $nodeId}})
            ON CREATE SET n += $props, n.createdAt = $now
            ON MATCH  SET n += $props
            RETURN n
            """,
            nodeId=node_id, props=props, now=_now_iso(),
        )
        record = await result.single()
        node_data = dict(record["n"])

    return {
        "nodeId":     node_id,
        "label":      label,
        "properties": {k: v for k, v in node_data.items() if k not in ("nodeId", "createdAt", "updatedAt")},
        "createdAt":  node_data.get("createdAt", _now_iso()),
        "updatedAt":  node_data.get("updatedAt", _now_iso()),
    }


async def _neo4j_add_relationship(from_id: str, to_id: str, rel_type: str, properties: Optional[dict] = None) -> dict:
    props = properties or {}
    async with _driver.session() as session:
        result = await session.run(
            f"""
            MATCH (a {{nodeId: $fromId}})
            MATCH (b {{nodeId: $toId}})
            MERGE (a)-[r:{rel_type}]->(b)
            ON CREATE SET r += $props, r.createdAt = $now, r.relationshipId = $relId
            RETURN r.relationshipId AS relId, r.createdAt AS createdAt
            """,
            fromId=from_id, toId=to_id, props=props,
            now=_now_iso(), relId=str(uuid.uuid4()),
        )
        record = await result.single()
        rel_id = record["relId"] if record else str(uuid.uuid4())
        created_at = record["createdAt"] if record else _now_iso()

    return {
        "relationshipId": rel_id,
        "from":       from_id,
        "to":         to_id,
        "type":       rel_type,
        "properties": props,
        "createdAt":  created_at,
    }


async def _neo4j_delete_node_and_rels(node_id: str) -> None:
    async with _driver.session() as session:
        await session.run(
            "MATCH (n {nodeId: $nodeId}) DETACH DELETE n",
            nodeId=node_id,
        )


async def _neo4j_list_nodes(label: Optional[str]) -> List[dict]:
    if label:
        # Whitelist prevents Cypher injection via label parameter
        if label not in ALLOWED_LABELS:
            raise ValueError(f"Unknown label: {label!r}. Allowed: {sorted(ALLOWED_LABELS)}")
        query = f"MATCH (n:`{label}`) RETURN n ORDER BY n.createdAt DESC"
    else:
        query = "MATCH (n) RETURN n ORDER BY n.createdAt DESC"
    async with _driver.session() as session:
        result = await session.run(query)
        records = await result.data()
    nodes = []
    for r in records:
        n = dict(r["n"])
        node_id = n.pop("nodeId", None)
        created_at = n.pop("createdAt", _now_iso())
        updated_at = n.pop("updatedAt", _now_iso())
        nodes.append({
            "nodeId":     node_id,
            "label":      label or "Unknown",
            "properties": n,
            "createdAt":  created_at,
            "updatedAt":  updated_at,
        })
    return nodes


async def _neo4j_get_node(node_id: str) -> Optional[dict]:
    async with _driver.session() as session:
        result = await session.run(
            "MATCH (n {nodeId: $nodeId}) RETURN n, labels(n) AS labels",
            nodeId=node_id,
        )
        record = await result.single()
    if record is None:
        return None
    n = dict(record["n"])
    labels = record["labels"]
    node_id_val = n.pop("nodeId", node_id)
    created_at = n.pop("createdAt", _now_iso())
    updated_at = n.pop("updatedAt", _now_iso())
    return {
        "nodeId":     node_id_val,
        "label":      labels[0] if labels else "Unknown",
        "properties": n,
        "createdAt":  created_at,
        "updatedAt":  updated_at,
    }


async def _neo4j_list_relationships(from_id: Optional[str], to_id: Optional[str], rel_type: Optional[str]) -> List[dict]:
    if rel_type:
        # Whitelist prevents Cypher injection via rel_type parameter
        if rel_type not in ALLOWED_REL_TYPES:
            raise ValueError(f"Unknown relationship type: {rel_type!r}. Allowed: {sorted(ALLOWED_REL_TYPES)}")
        rel_pattern = f"[r:`{rel_type}`]"
    else:
        rel_pattern = "[r]"
    conditions = []
    params: dict = {}
    if from_id:
        conditions.append("a.nodeId = $fromId"); params["fromId"] = from_id
    if to_id:
        conditions.append("b.nodeId = $toId"); params["toId"] = to_id
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    query = f"MATCH (a)-{rel_pattern}->(b) {where} RETURN r, a.nodeId AS fromId, b.nodeId AS toId, type(r) AS relType"
    async with _driver.session() as session:
        result = await session.run(query, **params)
        records = await result.data()
    rels = []
    for rec in records:
        r = dict(rec["r"])
        rels.append({
            "relationshipId": r.pop("relationshipId", str(uuid.uuid4())),
            "from":       rec["fromId"],
            "to":         rec["toId"],
            "type":       rec["relType"],
            "properties": r,
            "createdAt":  r.pop("createdAt", _now_iso()),
        })
    return rels


async def _neo4j_stats() -> dict:
    async with _driver.session() as session:
        r1 = await (await session.run("MATCH (n) RETURN count(n) AS cnt")).single()
        r2 = await (await session.run("MATCH ()-[r]->() RETURN count(r) AS cnt")).single()
        r3 = await (await session.run("MATCH (n) RETURN labels(n)[0] AS label, count(n) AS cnt")).data()
        r4 = await (await session.run("MATCH ()-[r]->() RETURN type(r) AS t, count(r) AS cnt")).data()
    return {
        "totalNodes":         r1["cnt"] if r1 else 0,
        "totalRelationships": r2["cnt"] if r2 else 0,
        "nodesByLabel":       {row["label"]: row["cnt"] for row in r3},
        "relationshipsByType":{row["t"]: row["cnt"] for row in r4},
    }


# ── In-memory path ───────────────────────────────────────────────────────────

def _mem_upsert_node(node_id: str, label: str, properties: dict) -> dict:
    node = {
        "nodeId":     node_id,
        "label":      label,
        "properties": properties,
        "createdAt":  _nodes.get(node_id, {}).get("createdAt", _now_iso()),
        "updatedAt":  _now_iso(),
    }
    _nodes[node_id] = node
    return node


def _mem_add_relationship(from_id: str, to_id: str, rel_type: str, properties: Optional[dict] = None) -> dict:
    for rel in _relationships:
        if rel["from"] == from_id and rel["to"] == to_id and rel["type"] == rel_type:
            return rel
    rel = {
        "relationshipId": str(uuid.uuid4()),
        "from":       from_id,
        "to":         to_id,
        "type":       rel_type,
        "properties": properties or {},
        "createdAt":  _now_iso(),
    }
    _relationships.append(rel)
    return rel


# ── Unified API ──────────────────────────────────────────────────────────────

async def _upsert_node(node_id: str, label: str, properties: dict) -> dict:
    if _driver:
        return await _neo4j_upsert_node(node_id, label, properties)
    return _mem_upsert_node(node_id, label, properties)


async def _add_relationship(from_id: str, to_id: str, rel_type: str, properties: Optional[dict] = None) -> dict:
    if _driver:
        return await _neo4j_add_relationship(from_id, to_id, rel_type, properties)
    return _mem_add_relationship(from_id, to_id, rel_type, properties)


async def _node_exists(node_id: str) -> bool:
    if _driver:
        node = await _neo4j_get_node(node_id)
        return node is not None
    return node_id in _nodes


# ---------------------------------------------------------------------------
# Event handler
# ---------------------------------------------------------------------------

async def handle_event(topic: str, value: dict) -> None:
    event_type = value.get("eventType", "unknown")
    logger.info("Received event: type=%s id=%s", event_type, value.get("eventId"))

    org_id    = value.get("organizationId", "")
    payload   = value.get("payload", {})
    nodes_created = 0
    rels_created  = 0

    if event_type == "RepositoryCreated":
        repo_id = payload.get("repositoryId", "")
        await _upsert_node(repo_id, "Repository", {
            "name":           payload.get("name"),
            "url":            payload.get("url"),
            "language":       payload.get("language"),
            "visibility":     payload.get("visibility"),
            "defaultBranch":  payload.get("defaultBranch"),
            "organizationId": org_id,
        })
        nodes_created = 1
        logger.info("Graph: created/updated Repository node %s", repo_id)

    elif event_type == "RepositoryUpdated":
        repo_id       = payload.get("repositoryId", "")
        changed_fields = payload.get("changedFields", {})

        if changed_fields:
            if _driver:
                # _upsert_node uses MERGE + ON MATCH SET so only the supplied
                # properties are updated; other node properties are untouched.
                await _upsert_node(repo_id, "Repository", changed_fields)
            else:
                # In-memory: patch only the changed properties into the
                # existing node so the full snapshot is preserved.
                if repo_id in _nodes:
                    _nodes[repo_id]["properties"].update(changed_fields)
                    _nodes[repo_id]["updatedAt"] = _now_iso()
            nodes_created = 0   # update, not creation
            logger.info(
                "Graph: updated Repository node %s — fields: %s",
                repo_id, list(changed_fields.keys()),
            )
        else:
            logger.info(
                "Graph: RepositoryUpdated for %s — no changedFields, skipping graph update",
                repo_id,
            )


    elif event_type == "RepositoryDeleted":
        repo_id = payload.get("repositoryId", "")
        if _driver:
            await _neo4j_delete_node_and_rels(repo_id)
        else:
            _nodes.pop(repo_id, None)
            global _relationships
            _relationships = [r for r in _relationships if r["from"] != repo_id and r["to"] != repo_id]
        logger.info("Graph: deleted Repository node %s and its relationships", repo_id)
        return  # No GraphUpdated event for deletion

    elif event_type == "DocumentProcessed":
        doc_id  = payload.get("documentId", "")
        repo_id = payload.get("repositoryId", "")
        await _upsert_node(doc_id, "Document", {
            "fileName":      payload.get("fileName"),
            "documentType":  payload.get("documentType"),
            "chunkCount":    payload.get("chunkCount"),
            "wordCount":     payload.get("wordCount"),
            "repositoryId":  repo_id,
        })
        nodes_created = 1

        if await _node_exists(repo_id):
            await _add_relationship(doc_id, repo_id, "DESCRIBES")
            rels_created = 1
            logger.info("Graph: Document(%s) -[DESCRIBES]-> Repository(%s)", doc_id, repo_id)
    else:
        return

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
    await _init_neo4j()
    await publisher.start()
    await subscriber.start(handle_event)
    yield
    await subscriber.stop()
    await publisher.stop()
    if _driver:
        await _driver.close()


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Graph Service",
    description="Builds and maintains the Living Knowledge Graph (Neo4j with in-memory fallback).",
    version="2.0.0",
    lifespan=lifespan,
)


@app.get("/health", tags=["Operations"])
def health_check():
    backend = "neo4j" if _driver else "in-memory"
    return {"status": "ok", "service": "graph-service", "backend": backend}


@app.get("/graph/nodes", tags=["Graph"])
async def list_nodes(label: Optional[str] = Query(None)):
    if label and label not in ALLOWED_LABELS:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown label: {label!r}. Allowed: {sorted(ALLOWED_LABELS)}"
        )
    if _driver:
        nodes = await _neo4j_list_nodes(label)
    else:
        nodes = list(_nodes.values())
        if label:
            nodes = [n for n in nodes if n["label"] == label]
    return {"data": nodes, "total": len(nodes)}


@app.get("/graph/nodes/{node_id}", tags=["Graph"])
async def get_node(node_id: str):
    if _driver:
        node = await _neo4j_get_node(node_id)
    else:
        node = _nodes.get(node_id)
    if node is None:
        raise HTTPException(status_code=404, detail=f"Node '{node_id}' not found.")
    return {"data": node}


@app.get("/graph/relationships", tags=["Graph"])
async def list_relationships(
    from_id:  Optional[str] = Query(None, alias="fromId"),
    to_id:    Optional[str] = Query(None, alias="toId"),
    rel_type: Optional[str] = Query(None, alias="type"),
):
    if rel_type and rel_type not in ALLOWED_REL_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown relationship type: {rel_type!r}. Allowed: {sorted(ALLOWED_REL_TYPES)}"
        )
    if _driver:
        rels = await _neo4j_list_relationships(from_id, to_id, rel_type)
    else:
        rels = _relationships[:]
        if from_id:  rels = [r for r in rels if r["from"] == from_id]
        if to_id:    rels = [r for r in rels if r["to"]   == to_id]
        if rel_type: rels = [r for r in rels if r["type"] == rel_type]
    return {"data": rels, "total": len(rels)}


@app.get("/graph/stats", tags=["Graph"])
async def graph_stats():
    if _driver:
        return await _neo4j_stats()
    labels = {}
    for node in _nodes.values():
        labels[node["label"]] = labels.get(node["label"], 0) + 1
    rel_types = {}
    for rel in _relationships:
        rel_types[rel["type"]] = rel_types.get(rel["type"], 0) + 1
    return {
        "totalNodes":          len(_nodes),
        "totalRelationships":  len(_relationships),
        "nodesByLabel":        labels,
        "relationshipsByType": rel_types,
    }
