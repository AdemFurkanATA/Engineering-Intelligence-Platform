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
    topics=[
        "repository.created", "repository.updated", "repository.deleted",
        "document.processed",
        # Phase 2
        "repository.cloned", "repository.clone_failed",
        "dependency.detected", "commit.analyzed",
    ],
)

# ---------------------------------------------------------------------------
# Security: Cypher label and relationship type whitelists
# Prevents injection via user-supplied label/type query parameters.
# ---------------------------------------------------------------------------
ALLOWED_LABELS: frozenset = frozenset({
    "Repository", "Document", "Developer", "Service",
    "Function", "Class", "API", "Database", "KafkaTopic",
    # Phase 2
    "Commit", "Dependency",
})
ALLOWED_REL_TYPES: frozenset = frozenset({
    "DESCRIBES", "DEPENDS_ON", "CALLS", "IMPLEMENTS",
    "PRODUCES", "CONSUMES", "MODIFIES", "REFERENCES",
    "CREATED_BY", "RELATED_TO", "BELONGS_TO",
    # Phase 2
    "AUTHORED_BY", "COMMITTED_TO", "DETECTED_IN",
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
        query = f"MATCH (n:`{label}`) RETURN n, labels(n) AS lbls ORDER BY n.createdAt DESC"
    else:
        # Include labels(n) so we always know the real label — without this
        # the response previously returned "Unknown" for every unfiltered node.
        query = "MATCH (n) RETURN n, labels(n) AS lbls ORDER BY n.createdAt DESC"
    async with _driver.session() as session:
        result = await session.run(query)
        records = await result.data()
    nodes = []
    for r in records:
        n = dict(r["n"])
        lbls = r.get("lbls", [])
        node_id    = n.pop("nodeId",    None)
        created_at = n.pop("createdAt", _now_iso())
        updated_at = n.pop("updatedAt", _now_iso())
        nodes.append({
            "nodeId":     node_id,
            # Use the queried label arg if provided (already validated),
            # otherwise fall back to the first label from Neo4j.
            "label":      label or (lbls[0] if lbls else "Unknown"),
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

    # ── Phase 2 handlers ────────────────────────────────────────────────────

    elif event_type == "RepositoryCloned":
        # Update the Repository node with clone metadata (commit count etc.)
        repo_id = payload.get("repositoryId", "")
        await _upsert_node(repo_id, "Repository", {
            "cloneStatus":  "success",
            "commitCount":  payload.get("commitCount", 0),
            "sizeKb":       payload.get("sizeKb", 0),
            "clonedAt":     payload.get("clonedAt", ""),
        })
        logger.info("Graph: Repository(%s) updated with clone metadata", repo_id)
        return  # No GraphUpdated event needed for metadata-only update

    elif event_type == "RepositoryCloneFailed":
        # Mark repository node so operators / UI know sync failed
        repo_id = payload.get("repositoryId", "")
        await _upsert_node(repo_id, "Repository", {
            "cloneStatus": "failed",
            "cloneError":  payload.get("error", "")[:200],
            "failedAt":    payload.get("failedAt", ""),
        })
        logger.warning("Graph: Repository(%s) clone FAILED — %s", repo_id, payload.get("error", "")[:80])
        return  # No GraphUpdated event needed

    elif event_type == "DependencyDetected":
        repo_id = payload.get("repositoryId", "")
        dep_name = payload.get("name", "")
        dep_ver  = payload.get("version", "")
        ecosystem = payload.get("ecosystem", "unknown")

        # Use name+ecosystem as stable node ID to deduplicate across repos
        dep_id = f"dep:{ecosystem}:{dep_name}:{dep_ver}"
        await _upsert_node(dep_id, "Dependency", {
            "name":       dep_name,
            "version":    dep_ver,
            "ecosystem":  ecosystem,
            "sourceFile": payload.get("sourceFile", ""),
        })
        nodes_created = 1

        if await _node_exists(repo_id):
            await _add_relationship(repo_id, dep_id, "DEPENDS_ON")
            rels_created = 1
            logger.info(
                "Graph: Repository(%s) -[DEPENDS_ON]-> Dependency(%s@%s, %s)",
                repo_id, dep_name, dep_ver, ecosystem,
            )

    elif event_type == "CommitAnalyzed":
        repo_id      = payload.get("repositoryId", "")
        sha          = payload.get("sha", "")
        author_email = payload.get("authorEmail", "")
        author_name  = payload.get("authorName", "")
        commit_id    = f"commit:{sha}"
        dev_id       = f"developer:{author_email}"

        # Create Commit node
        await _upsert_node(commit_id, "Commit", {
            "sha":          sha,
            "message":      (payload.get("message") or "")[:200],
            "authorEmail":  author_email,
            "authorName":   author_name,
            "committedAt":  payload.get("committedAt", ""),
            "filesChanged": len(payload.get("filesChanged", [])),
            "repositoryId": repo_id,
        })
        nodes_created = 1

        # Create Developer node (MERGE → idempotent)
        if author_email:
            await _upsert_node(dev_id, "Developer", {
                "email": author_email,
                "name":  author_name,
            })
            await _add_relationship(dev_id, commit_id, "AUTHORED_BY")
            nodes_created += 1
            rels_created  += 1

        # Link commit to repository
        if await _node_exists(repo_id):
            await _add_relationship(commit_id, repo_id, "COMMITTED_TO")
            rels_created += 1

        logger.info(
            "Graph: Commit(%s) by %s in Repository(%s)",
            sha[:8], author_email, repo_id,
        )

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


# ---------------------------------------------------------------------------
# Dependency Analyzer helpers
# ---------------------------------------------------------------------------

def _mem_dep_analysis(repo_id: str) -> dict:
    """Analyse dependency graph in in-memory mode."""
    # All DEPENDS_ON edges where source == repo_id
    dep_ids = {
        r["to"] for r in _relationships
        if r["from"] == repo_id and r["type"] == "DEPENDS_ON"
    }
    if not dep_ids:
        return {
            "repositoryId":        repo_id,
            "totalDependencies":   0,
            "circularDependencies": [],
            "criticalNodes":       [],
            "bottleneckScore":     0.0,
            "ecosystemBreakdown":  {},
        }

    # Build adjacency for deps (dep → repos that use it, for in-degree)
    dep_in_degree: dict[str, int] = {}
    for dep_id in dep_ids:
        count = sum(
            1 for r in _relationships
            if r["to"] == dep_id and r["type"] == "DEPENDS_ON"
        )
        dep_in_degree[dep_id] = count

    max_in_degree = max(dep_in_degree.values(), default=0)

    # Circular dependency: in in-memory MVP, detect if any dep node
    # also has a DEPENDS_ON edge pointing back (simplified check).
    circular = []
    for dep_id in dep_ids:
        for r in _relationships:
            if r["from"] == dep_id and r["to"] == repo_id and r["type"] == "DEPENDS_ON":
                dep_props = _nodes.get(dep_id, {}).get("properties", {})
                circular.append(dep_props.get("name", dep_id))

    # Critical nodes: in-degree >= 2
    critical = []
    total_repos = max(
        len({r["from"] for r in _relationships if r["type"] == "DEPENDS_ON"}), 1
    )
    for dep_id, in_deg in sorted(dep_in_degree.items(), key=lambda x: -x[1]):
        if in_deg >= 2:
            props = _nodes.get(dep_id, {}).get("properties", {})
            critical.append({
                "name":      props.get("name", dep_id),
                "version":   props.get("version", ""),
                "ecosystem": props.get("ecosystem", "unknown"),
                "inDegree":  in_deg,
                "score":     round(in_deg / total_repos, 4),
            })

    bottleneck_score = round(max_in_degree / total_repos, 4) if total_repos else 0.0

    # Ecosystem breakdown
    ecosystem_counts: dict[str, int] = {}
    for dep_id in dep_ids:
        eco = _nodes.get(dep_id, {}).get("properties", {}).get("ecosystem", "unknown")
        ecosystem_counts[eco] = ecosystem_counts.get(eco, 0) + 1

    return {
        "repositoryId":        repo_id,
        "totalDependencies":   len(dep_ids),
        "circularDependencies": circular,
        "criticalNodes":       critical,
        "bottleneckScore":     bottleneck_score,
        "ecosystemBreakdown":  ecosystem_counts,
    }


async def _neo4j_dep_analysis(repo_id: str) -> dict:
    """Analyse dependency graph using Neo4j Cypher."""
    async with _driver.session() as session:
        # Per-dependency in-degree across all repos
        result = await session.run(
            """
            MATCH (r:Repository {nodeId: $repoId})-[:DEPENDS_ON]->(d:Dependency)
            OPTIONAL MATCH (other:Repository)-[:DEPENDS_ON]->(d)
            WITH d, count(DISTINCT other) AS inDegree
            RETURN d.nodeId AS depId, d.name AS name, d.version AS version,
                   d.ecosystem AS ecosystem, inDegree
            ORDER BY inDegree DESC
            """,
            repoId=repo_id,
        )
        rows = await result.data()

    if not rows:
        return {
            "repositoryId":        repo_id,
            "totalDependencies":   0,
            "circularDependencies": [],
            "criticalNodes":       [],
            "bottleneckScore":     0.0,
            "ecosystemBreakdown":  {},
        }

    total_repos_result = await _driver.session().__aenter__()
    async with _driver.session() as session:
        r2 = await (await session.run(
            "MATCH (r:Repository) RETURN count(r) AS cnt"
        )).single()
    total_repos = max((r2["cnt"] if r2 else 1), 1)

    max_in_degree = max((row["inDegree"] for row in rows), default=0)
    bottleneck_score = round(max_in_degree / total_repos, 4)

    critical = [
        {
            "name":      row["name"],
            "version":   row["version"] or "",
            "ecosystem": row["ecosystem"] or "unknown",
            "inDegree":  row["inDegree"],
            "score":     round(row["inDegree"] / total_repos, 4),
        }
        for row in rows if row["inDegree"] >= 2
    ]

    ecosystem_counts: dict[str, int] = {}
    for row in rows:
        eco = row["ecosystem"] or "unknown"
        ecosystem_counts[eco] = ecosystem_counts.get(eco, 0) + 1

    # Circular deps: repos that appear as both source and target in DEPENDS_ON paths
    async with _driver.session() as session:
        circ_result = await session.run(
            """
            MATCH (r:Repository {nodeId: $repoId})-[:DEPENDS_ON]->(d:Dependency)
            -[:DEPENDS_ON]->(r)
            RETURN d.name AS name
            """,
            repoId=repo_id,
        )
        circ_rows = await circ_result.data()
    circular = [row["name"] for row in circ_rows]

    return {
        "repositoryId":        repo_id,
        "totalDependencies":   len(rows),
        "circularDependencies": circular,
        "criticalNodes":       critical,
        "bottleneckScore":     bottleneck_score,
        "ecosystemBreakdown":  ecosystem_counts,
    }


# ---------------------------------------------------------------------------
# Dependency Analyzer endpoints
# NOTE: Static paths (/critical, /ecosystem) MUST be registered before the
# parametric /{repo_id} path, otherwise FastAPI will match "critical" and
# "ecosystem" as repo_id values.
# ---------------------------------------------------------------------------

@app.get("/graph/analysis/dependencies/critical", tags=["Analysis"])
async def critical_dependencies(threshold: int = Query(default=2, ge=1)):
    """List the most-used dependencies across all repositories.

    A dependency is 'critical' if it is used by at least `threshold` repositories.
    """
    if _driver:
        async with _driver.session() as session:
            result = await session.run(
                """
                MATCH (r:Repository)-[:DEPENDS_ON]->(d:Dependency)
                WITH d, count(DISTINCT r) AS usedBy
                WHERE usedBy >= $threshold
                RETURN d.name AS name, d.version AS version,
                       d.ecosystem AS ecosystem, usedBy
                ORDER BY usedBy DESC
                """,
                threshold=threshold,
            )
            rows = await result.data()
        async with _driver.session() as session:
            tr = await (await session.run("MATCH (r:Repository) RETURN count(r) AS cnt")).single()
        total_repos = (tr["cnt"] if tr else 1) or 1
        deps = [
            {
                "name":             row["name"],
                "version":          row["version"] or "",
                "ecosystem":        row["ecosystem"] or "unknown",
                "usedByRepos":      row["usedBy"],
                "criticalityScore": round(row["usedBy"] / total_repos, 4),
            }
            for row in rows
        ]
    else:
        # In-memory path
        dep_counts: dict[str, int] = {}
        for r in _relationships:
            if r["type"] == "DEPENDS_ON":
                dep_counts[r["to"]] = dep_counts.get(r["to"], 0) + 1
        total_repos = max(
            len({r["from"] for r in _relationships if r["type"] == "DEPENDS_ON"}), 1
        )
        deps = []
        for dep_id, count in sorted(dep_counts.items(), key=lambda x: -x[1]):
            if count >= threshold:
                props = _nodes.get(dep_id, {}).get("properties", {})
                deps.append({
                    "name":             props.get("name", dep_id),
                    "version":          props.get("version", ""),
                    "ecosystem":        props.get("ecosystem", "unknown"),
                    "usedByRepos":      count,
                    "criticalityScore": round(count / total_repos, 4),
                })

    return {
        "criticalDependencies": deps,
        "threshold":            threshold,
        "analyzedAt":           _now_iso(),
    }


@app.get("/graph/analysis/dependencies/ecosystem", tags=["Analysis"])
async def ecosystem_breakdown():
    """Return dependency ecosystem distribution across the entire organisation."""
    if _driver:
        async with _driver.session() as session:
            result = await session.run(
                """
                MATCH (d:Dependency)
                RETURN d.ecosystem AS ecosystem, count(d) AS cnt
                ORDER BY cnt DESC
                """
            )
            rows = await result.data()
        ecosystems = {(row["ecosystem"] or "unknown"): row["cnt"] for row in rows}
        total = sum(ecosystems.values())
        async with _driver.session() as session:
            ur = await (await session.run(
                "MATCH (d:Dependency) RETURN count(DISTINCT d.name) AS cnt"
            )).single()
        unique = ur["cnt"] if ur else 0
    else:
        ecosystems: dict[str, int] = {}
        unique_names: set[str] = set()
        for node in _nodes.values():
            if node["label"] == "Dependency":
                eco = node["properties"].get("ecosystem", "unknown")
                ecosystems[eco] = ecosystems.get(eco, 0) + 1
                unique_names.add(node["properties"].get("name", ""))
        total = sum(ecosystems.values())
        unique = len(unique_names)

    return {
        "ecosystems":         ecosystems,
        "totalDependencies":  total,
        "uniquePackages":     unique,
        "analyzedAt":         _now_iso(),
    }


@app.get("/graph/analysis/dependencies/{repo_id}", tags=["Analysis"])
async def analyze_dependencies(repo_id: str):
    """Analyse the dependency graph for a single repository.

    Returns circular dependency detection, critical nodes (high in-degree),
    bottleneck score, and ecosystem breakdown.
    """
    if not await _node_exists(repo_id):
        raise HTTPException(status_code=404, detail=f"Repository '{repo_id}' not found in graph.")

    if _driver:
        result = await _neo4j_dep_analysis(repo_id)
    else:
        result = _mem_dep_analysis(repo_id)

    result["analyzedAt"] = _now_iso()
    return result


# ---------------------------------------------------------------------------
# Timeline Engine helpers
# ---------------------------------------------------------------------------

def _mem_timeline(repo_id: str, since: Optional[str], until: Optional[str],
                  limit: int, event_type_filter: str) -> list:
    """Build timeline from in-memory graph for a single repo."""
    events = []

    if event_type_filter in ("all", "commit"):
        for node in _nodes.values():
            if node["label"] != "Commit":
                continue
            props = node["properties"]
            if props.get("repositoryId") != repo_id:
                continue
            ts = props.get("committedAt", "")
            if since and ts and ts < since:
                continue
            if until and ts and ts > until:
                continue
            events.append({
                "type":        "commit",
                "timestamp":   ts,
                "sha":         props.get("sha", ""),
                "message":     props.get("message", ""),
                "authorEmail": props.get("authorEmail", ""),
                "authorName":  props.get("authorName", ""),
                "filesChanged": props.get("filesChanged", 0),
            })

    if event_type_filter in ("all", "dependency"):
        for node in _nodes.values():
            if node["label"] != "Dependency":
                continue
            # Check if this dep is linked to the repo
            linked = any(
                r["from"] == repo_id and r["to"] == node["nodeId"]
                and r["type"] == "DEPENDS_ON"
                for r in _relationships
            )
            if not linked:
                continue
            ts = node.get("createdAt", "")
            if since and ts and ts < since:
                continue
            if until and ts and ts > until:
                continue
            props = node["properties"]
            events.append({
                "type":           "dependency_added",
                "timestamp":      ts,
                "dependencyName": props.get("name", ""),
                "ecosystem":      props.get("ecosystem", "unknown"),
                "version":        props.get("version", ""),
            })

    # Sort by timestamp descending
    events.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
    return events[:limit]


async def _neo4j_timeline(repo_id: str, since: Optional[str], until: Optional[str],
                           limit: int, event_type_filter: str) -> list:
    """Build timeline from Neo4j for a single repo."""
    events = []

    if event_type_filter in ("all", "commit"):
        params: dict = {"repoId": repo_id, "limit": limit}
        conditions = ["c.repositoryId = $repoId"]
        if since:
            conditions.append("c.committedAt >= $since")
            params["since"] = since
        if until:
            conditions.append("c.committedAt <= $until")
            params["until"] = until
        where = "WHERE " + " AND ".join(conditions)
        async with _driver.session() as session:
            result = await session.run(
                f"""
                MATCH (c:Commit)
                {where}
                RETURN c.sha AS sha, c.message AS message,
                       c.authorEmail AS authorEmail, c.authorName AS authorName,
                       c.committedAt AS committedAt, c.filesChanged AS filesChanged
                ORDER BY c.committedAt DESC
                LIMIT $limit
                """,
                **params,
            )
            rows = await result.data()
        for row in rows:
            events.append({
                "type":        "commit",
                "timestamp":   row.get("committedAt", ""),
                "sha":         row.get("sha", ""),
                "message":     row.get("message", ""),
                "authorEmail": row.get("authorEmail", ""),
                "authorName":  row.get("authorName", ""),
                "filesChanged": row.get("filesChanged", 0),
            })

    if event_type_filter in ("all", "dependency"):
        dep_params: dict = {"repoId": repo_id, "limit": limit}
        async with _driver.session() as session:
            result = await session.run(
                """
                MATCH (r:Repository {nodeId: $repoId})-[:DEPENDS_ON]->(d:Dependency)
                RETURN d.name AS name, d.ecosystem AS ecosystem,
                       d.version AS version, d.createdAt AS createdAt
                ORDER BY d.createdAt DESC
                LIMIT $limit
                """,
                **dep_params,
            )
            rows = await result.data()
        for row in rows:
            ts = row.get("createdAt", "")
            if since and ts and ts < since:
                continue
            if until and ts and ts > until:
                continue
            events.append({
                "type":           "dependency_added",
                "timestamp":      ts,
                "dependencyName": row.get("name", ""),
                "ecosystem":      row.get("ecosystem", "unknown"),
                "version":        row.get("version", ""),
            })

    events.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
    return events[:limit]


# ---------------------------------------------------------------------------
# Timeline Engine endpoint
# ---------------------------------------------------------------------------

@app.get("/graph/timeline/{repo_id}", tags=["Analysis"])
async def repository_timeline(
    repo_id: str,
    since:      Optional[str] = Query(None,   description="ISO datetime lower bound"),
    until:      Optional[str] = Query(None,   description="ISO datetime upper bound"),
    limit:      int           = Query(50,     ge=1, le=500),
    event_type: str           = Query("all",  description="'commit' | 'dependency' | 'all'"),
):
    """Return a time-ordered event stream for a repository.

    Merges commits and dependency additions into a single chronological list.
    Supports filtering by time range and event type.
    """
    if event_type not in ("all", "commit", "dependency"):
        raise HTTPException(
            status_code=422,
            detail="event_type must be 'all', 'commit', or 'dependency'",
        )

    if not await _node_exists(repo_id):
        raise HTTPException(status_code=404, detail=f"Repository '{repo_id}' not found in graph.")

    if _driver:
        events = await _neo4j_timeline(repo_id, since, until, limit, event_type)
    else:
        events = _mem_timeline(repo_id, since, until, limit, event_type)

    return {
        "repositoryId": repo_id,
        "events":       events,
        "total":        len(events),
        "since":        since,
        "until":        until,
        "eventType":    event_type,
        "generatedAt":  _now_iso(),
    }
