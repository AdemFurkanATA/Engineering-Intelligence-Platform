"""
impact_analyzer.py — Change impact analysis via graph traversal.

Given an entity and a change type (modify | delete | add),
performs a BFS/DFS traversal of outgoing relationships to determine
which other entities are affected and at what risk level.

Risk levels
-----------
  critical : entity is on a critical path (Kafka topic, public API, Service)
  high     : entity has many dependents (high in-degree)
  medium   : transitively affected
  low      : weakly connected

Output structure
----------------
  {
    "affectedEntities": [
      {"id", "type", "name", "riskLevel", "distance", "path"}
    ],
    "riskSummary":  {"critical": N, "high": N, "medium": N, "low": N},
    "criticalPaths": [[entity_id, ...], ...],
    "totalAffected": N,
    "maxDepth":      N,
  }
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_DEPTH = 3
_DEFAULT_MAX_AFFECTED = 200

# Node types considered "critical" regardless of degree
_CRITICAL_TYPES = {"KafkaTopic", "ApiEndpoint", "Service", "Database"}

# Relationship types that propagate impact (outgoing = "I depend on")
_IMPACT_RELS = {
    "DEPENDS_ON", "CALLS", "IMPLEMENTS", "CONSUMES", "PRODUCES",
    "READS_FROM", "WRITES_TO", "ROUTES_TO", "EXPOSES",
}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class AffectedEntity:
    entity_id:  str
    entity_type: str
    entity_name: str
    risk_level: str
    distance:   int
    path:       list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id":        self.entity_id,
            "type":      self.entity_type,
            "name":      self.entity_name,
            "riskLevel": self.risk_level,
            "distance":  self.distance,
            "path":      self.path,
        }


@dataclass
class ImpactReport:
    source_id:        str
    change_type:      str
    affected_entities: list[AffectedEntity] = field(default_factory=list)
    critical_paths:   list[list[str]]       = field(default_factory=list)
    max_depth:        int = 0

    @property
    def risk_summary(self) -> dict[str, int]:
        counts: dict[str, int] = {"critical": 0, "high": 0, "medium": 0, "low": 0}
        for e in self.affected_entities:
            counts[e.risk_level] = counts.get(e.risk_level, 0) + 1
        return counts

    def to_dict(self) -> dict:
        return {
            "sourceId":        self.source_id,
            "changeType":      self.change_type,
            "totalAffected":   len(self.affected_entities),
            "maxDepth":        self.max_depth,
            "riskSummary":     self.risk_summary,
            "affectedEntities": [e.to_dict() for e in self.affected_entities],
            "criticalPaths":   self.critical_paths,
        }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def analyze_impact(
    source_id:   str,
    graph_data:  dict,
    *,
    change_type: str = "modify",
    depth:       int = _DEFAULT_DEPTH,
    max_affected: int = _DEFAULT_MAX_AFFECTED,
) -> ImpactReport:
    """Compute the ripple-effect impact of changing an entity.

    Args:
        source_id:    ID of the entity being changed.
        graph_data:   Dict with 'nodes' and 'relationships'.
        change_type:  "modify" | "delete" | "add"
        depth:        BFS depth limit.
        max_affected: Cap on number of affected entities returned.

    Returns:
        ImpactReport with all affected entities and risk levels.
    """
    nodes         = graph_data.get("nodes", {})
    relationships = graph_data.get("relationships", [])

    # Build adjacency: who depends on X (reverse direction = "who will be affected")
    # Impact propagates to callers/dependents, not to dependencies
    # → we traverse INCOMING edges (who calls/depends on the changed entity)
    reverse_adj: dict[str, list[tuple[str, str]]] = {nid: [] for nid in nodes}
    for rel in relationships:
        rel_type = rel.get("type", "")
        if rel_type not in _IMPACT_RELS:
            continue
        src = rel.get("sourceId", "")
        tgt = rel.get("targetId", "")
        # src DEPENDS_ON tgt → if tgt changes, src is affected
        if tgt in reverse_adj:
            reverse_adj[tgt].append((src, rel_type))

    # Also traverse forward for "delete" (entity disappears, its dependents break)
    forward_adj: dict[str, list[tuple[str, str]]] = {nid: [] for nid in nodes}
    for rel in relationships:
        rel_type = rel.get("type", "")
        if rel_type not in _IMPACT_RELS:
            continue
        src = rel.get("sourceId", "")
        tgt = rel.get("targetId", "")
        if src in forward_adj:
            forward_adj[src].append((tgt, rel_type))

    # Compute in-degree for risk scoring
    in_degree: dict[str, int] = {nid: 0 for nid in nodes}
    for rel in relationships:
        tgt = rel.get("targetId", "")
        if tgt in in_degree:
            in_degree[tgt] += 1

    # BFS from source_id traversing reverse adjacency (who depends on this entity)
    visited: dict[str, int] = {}  # node_id → distance
    path_map: dict[str, list[str]] = {source_id: [source_id]}
    queue: deque[tuple[str, int]] = deque([(source_id, 0)])
    affected: list[AffectedEntity] = []
    critical_paths: list[list[str]] = []
    actual_max_depth = 0

    while queue and len(affected) < max_affected:
        current, dist = queue.popleft()
        if current in visited:
            continue
        visited[current] = dist
        actual_max_depth = max(actual_max_depth, dist)

        if current != source_id:
            node      = nodes.get(current, {})
            node_type = node.get("label", "Unknown")
            props     = node.get("properties", {})
            name      = props.get("name") or props.get("title") or current
            risk      = _compute_risk(current, node_type, dist, in_degree, change_type)

            path = list(path_map.get(current, [current]))
            ae = AffectedEntity(
                entity_id=current,
                entity_type=node_type,
                entity_name=name,
                risk_level=risk,
                distance=dist,
                path=path,
            )
            affected.append(ae)

            if risk == "critical":
                critical_paths.append(path)

        if dist < depth:
            for (neighbour, rel_type) in reverse_adj.get(current, []):
                if neighbour not in visited:
                    parent_path = path_map.get(current, [current])
                    path_map[neighbour] = parent_path + [neighbour]
                    queue.append((neighbour, dist + 1))

    # Sort by risk level then distance
    risk_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    affected.sort(key=lambda e: (risk_order.get(e.risk_level, 4), e.distance))

    return ImpactReport(
        source_id=source_id,
        change_type=change_type,
        affected_entities=affected,
        critical_paths=critical_paths[:10],
        max_depth=actual_max_depth,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _compute_risk(
    node_id:     str,
    node_type:   str,
    distance:    int,
    in_degree:   dict[str, int],
    change_type: str,
) -> str:
    """Assign a risk level to an affected entity."""
    # Critical node types always get critical risk
    if node_type in _CRITICAL_TYPES:
        return "critical"

    degree = in_degree.get(node_id, 0)

    # Delete changes are more risky (breakage vs behaviour change)
    delete_mult = 1 if change_type != "delete" else 2

    if degree * delete_mult >= 5 or (degree >= 3 and distance == 1):
        return "high"
    if distance == 1 and degree >= 1:
        return "medium"
    if distance <= 2:
        return "medium"
    return "low"
