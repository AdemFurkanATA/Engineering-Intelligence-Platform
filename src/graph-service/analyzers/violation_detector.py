"""
violation_detector.py — Architectural violation detection for the Engineering Intelligence Platform.

Violations detected (rule-based, deterministic):
  - god_class       : Class node with method count > threshold (default 20)
  - cycle           : Circular DEPENDS_ON / CALLS relationships (DFS)
  - orphan_module   : Node with zero relationships
  - oversized_service : Service node with too many direct Function/Class children
  - layer_violation : Dependency flows against the natural layer direction
                      (e.g., model → controller)
  - dead_code       : Function with in-degree=0 and no direct callers
                      (candidate — requires age metadata to confirm)

Each violation carries a severity (critical / high / medium / low) and a
suggested remediation action.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Configuration thresholds (can be overridden per call)
# ---------------------------------------------------------------------------

DEFAULT_GOD_CLASS_THRESHOLD      = 20   # methods per class
DEFAULT_SERVICE_SIZE_THRESHOLD   = 50   # functions/classes per service
DEFAULT_CYCLE_MAX_DEPTH          = 10   # DFS depth limit for cycle detection


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Violation:
    violation_type: str
    severity:       str   # critical | high | medium | low
    entity_id:      str
    entity_name:    str
    description:    str
    recommendation: str
    related:        list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "type":           self.violation_type,
            "severity":       self.severity,
            "entityId":       self.entity_id,
            "entityName":     self.entity_name,
            "description":    self.description,
            "recommendation": self.recommendation,
            "related":        self.related,
        }


@dataclass
class ViolationReport:
    violations:     list[Violation] = field(default_factory=list)
    summary:        dict            = field(default_factory=dict)

    def to_dict(self) -> dict:
        counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
        for v in self.violations:
            counts[v.severity] = counts.get(v.severity, 0) + 1
        return {
            "violations": [v.to_dict() for v in self.violations],
            "totalCount": len(self.violations),
            "bySeverity": counts,
        }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def detect_violations(
    graph_data: dict,
    god_class_threshold:    int = DEFAULT_GOD_CLASS_THRESHOLD,
    service_size_threshold: int = DEFAULT_SERVICE_SIZE_THRESHOLD,
) -> ViolationReport:
    """Detect architectural violations in the graph.

    Args:
        graph_data: Dict with keys:
            - nodes: dict[node_id, {label, properties}]
            - relationships: list[{sourceId, targetId, type}]

    Returns:
        ViolationReport with all found violations.
    """
    nodes         = graph_data.get("nodes", {})
    relationships = graph_data.get("relationships", [])

    violations: list[Violation] = []

    violations.extend(_detect_god_classes(nodes, relationships, god_class_threshold))
    violations.extend(_detect_cycles(nodes, relationships))
    violations.extend(_detect_orphans(nodes, relationships))
    violations.extend(_detect_oversized_services(nodes, relationships, service_size_threshold))
    violations.extend(_detect_layer_violations(nodes, relationships))
    violations.extend(_detect_dead_code_candidates(nodes, relationships))

    return ViolationReport(violations=violations)


# ---------------------------------------------------------------------------
# Individual detectors
# ---------------------------------------------------------------------------

def _get_name(node: dict) -> str:
    props = node.get("properties", {})
    return props.get("name") or props.get("title") or ""


def _detect_god_classes(
    nodes: dict, relationships: list[dict], threshold: int
) -> list[Violation]:
    """Flag Class nodes that have too many CALLS/IMPLEMENTS outgoing edges."""
    violations = []

    # Count outgoing edges per node
    out_degree: dict[str, int] = {}
    for rel in relationships:
        src = rel.get("sourceId", "")
        out_degree[src] = out_degree.get(src, 0) + 1

    for nid, node in nodes.items():
        if node.get("label") != "Class":
            continue
        props       = node.get("properties", {})
        method_count = props.get("methodCount", 0) or out_degree.get(nid, 0)
        if method_count > threshold:
            name = _get_name(node) or nid
            violations.append(Violation(
                violation_type="god_class",
                severity="high",
                entity_id=nid,
                entity_name=name,
                description=(
                    f"Class '{name}' has {method_count} methods/relations "
                    f"(threshold: {threshold}). God classes violate SRP and "
                    "become maintenance bottlenecks."
                ),
                recommendation=(
                    "Decompose into smaller, focused classes. "
                    "Extract cohesive groups of methods into separate classes or services."
                ),
            ))

    return violations


def _detect_cycles(nodes: dict, relationships: list[dict]) -> list[Violation]:
    """Detect circular DEPENDS_ON relationships using iterative DFS."""
    violations = []

    # Build adjacency list for DEPENDS_ON / CALLS
    adj: dict[str, list[str]] = {nid: [] for nid in nodes}
    for rel in relationships:
        if rel.get("type") in ("DEPENDS_ON", "CALLS"):
            src = rel.get("sourceId", "")
            tgt = rel.get("targetId", "")
            if src in adj:
                adj[src].append(tgt)

    # DFS cycle detection — returns list of cycle paths
    visited  = set()
    in_stack = set()
    cycles_found: list[tuple[str, ...]] = []

    def _dfs(node: str, path: list[str]) -> None:
        if node in in_stack:
            # Found a cycle — extract the cycle portion
            idx = path.index(node)
            cycle = tuple(path[idx:])
            if cycle not in cycles_found:
                cycles_found.append(cycle)
            return
        if node in visited:
            return
        visited.add(node)
        in_stack.add(node)
        path.append(node)
        for neighbour in adj.get(node, []):
            if len(path) < DEFAULT_CYCLE_MAX_DEPTH:
                _dfs(neighbour, path)
        path.pop()
        in_stack.discard(node)

    for nid in list(nodes.keys()):
        if nid not in visited:
            _dfs(nid, [])

    for cycle in cycles_found[:10]:  # cap at 10 reported cycles
        names = []
        for nid in cycle:
            n = nodes.get(nid, {})
            names.append(_get_name(n) or nid)
        cycle_str = " → ".join(names) + f" → {names[0]}"
        violations.append(Violation(
            violation_type="dependency_cycle",
            severity="high",
            entity_id=cycle[0],
            entity_name=names[0],
            description=f"Circular dependency detected: {cycle_str}",
            recommendation=(
                "Introduce an abstraction layer or event-based decoupling "
                "to break the cycle."
            ),
            related=list(cycle[1:]),
        ))

    return violations


def _detect_orphans(nodes: dict, relationships: list[dict]) -> list[Violation]:
    """Flag nodes that have zero relationships (completely isolated)."""
    violations = []

    connected: set[str] = set()
    for rel in relationships:
        connected.add(rel.get("sourceId", ""))
        connected.add(rel.get("targetId", ""))

    ignore_labels = {"Repository", "Developer"}  # these legitimately start with 0 rels

    for nid, node in nodes.items():
        label = node.get("label", "")
        if label in ignore_labels:
            continue
        if nid not in connected:
            name = _get_name(node) or nid
            violations.append(Violation(
                violation_type="orphan_node",
                severity="low",
                entity_id=nid,
                entity_name=name,
                description=(
                    f"{label} '{name}' has no relationships to any other node. "
                    "It may be dead code, a misconfigured entry, or a missing integration."
                ),
                recommendation=(
                    "Verify whether this node is intentionally isolated. "
                    "If it's unreachable code, consider removing it."
                ),
            ))

    return violations


def _detect_oversized_services(
    nodes: dict, relationships: list[dict], threshold: int
) -> list[Violation]:
    """Flag Service nodes that directly contain too many Functions or Classes."""
    violations = []

    # Count how many Function/Class nodes belong to each Service
    service_children: dict[str, int] = {}
    for rel in relationships:
        if rel.get("type") in ("BELONGS_TO", "DETECTED_IN"):
            tgt = rel.get("targetId", "")
            if tgt in nodes and nodes[tgt].get("label") == "Service":
                service_children[tgt] = service_children.get(tgt, 0) + 1

    for nid, count in service_children.items():
        if count > threshold:
            node = nodes.get(nid, {})
            name = _get_name(node) or nid
            violations.append(Violation(
                violation_type="oversized_service",
                severity="medium",
                entity_id=nid,
                entity_name=name,
                description=(
                    f"Service '{name}' contains {count} code entities "
                    f"(threshold: {threshold}). "
                    "Oversized services are hard to understand and maintain."
                ),
                recommendation=(
                    "Consider splitting into focused sub-services or "
                    "introducing internal module boundaries."
                ),
            ))

    return violations


# Layer precedence: index 0 = outermost (controller), index 3 = innermost (model)
_LAYER_ORDER = ["controller", "service", "repository", "model"]
_LAYER_PATTERNS = {
    "controller":  {"controller", "controllers", "handler", "handlers",
                    "route", "routes", "resource", "api"},
    "service":     {"service", "services", "usecase", "usecases", "application"},
    "repository":  {"repository", "repositories", "repo", "store", "storage", "dao"},
    "model":       {"model", "models", "entity", "entities", "domain"},
}


def _classify_layer(name: str) -> Optional[str]:
    name_lower = name.lower()
    for layer, keywords in _LAYER_PATTERNS.items():
        if any(kw in name_lower for kw in keywords):
            return layer
    return None


def _detect_layer_violations(nodes: dict, relationships: list[dict]) -> list[Violation]:
    """Detect dependencies flowing against natural layer direction."""
    violations = []

    node_layers: dict[str, str] = {}
    for nid, node in nodes.items():
        name  = _get_name(node) or nid
        layer = _classify_layer(name)
        if layer:
            node_layers[nid] = layer

    for rel in relationships:
        if rel.get("type") not in ("DEPENDS_ON", "CALLS"):
            continue
        src = rel.get("sourceId", "")
        tgt = rel.get("targetId", "")
        src_layer = node_layers.get(src)
        tgt_layer = node_layers.get(tgt)
        if not src_layer or not tgt_layer:
            continue
        src_idx = _LAYER_ORDER.index(src_layer)
        tgt_idx = _LAYER_ORDER.index(tgt_layer)
        # Violation: inner layer depends on outer layer
        if tgt_idx < src_idx:
            src_name = _get_name(nodes.get(src, {})) or src
            tgt_name = _get_name(nodes.get(tgt, {})) or tgt
            violations.append(Violation(
                violation_type="layer_violation",
                severity="medium",
                entity_id=src,
                entity_name=src_name,
                description=(
                    f"Layer violation: '{src_name}' ({src_layer}) → "
                    f"'{tgt_name}' ({tgt_layer}). "
                    "Inner layers should not depend on outer layers."
                ),
                recommendation=(
                    "Invert the dependency using an interface/port in the inner layer. "
                    "The outer layer should implement the interface."
                ),
                related=[tgt],
            ))

    return violations


def _detect_dead_code_candidates(nodes: dict, relationships: list[dict]) -> list[Violation]:
    """Flag Function nodes with no incoming CALLS edges (potential dead code)."""
    violations = []

    # Compute in-degree (callers) for Function nodes
    in_degree: dict[str, int] = {}
    for nid in nodes:
        in_degree[nid] = 0
    for rel in relationships:
        if rel.get("type") == "CALLS":
            tgt = rel.get("targetId", "")
            in_degree[tgt] = in_degree.get(tgt, 0) + 1

    for nid, node in nodes.items():
        if node.get("label") != "Function":
            continue
        props = node.get("properties", {})
        name  = _get_name(node) or nid
        # Skip constructors, __init__, and test functions
        if any(pat in name.lower() for pat in ("__init__", "test_", "setUp", "tearDown")):
            continue
        if in_degree.get(nid, 0) == 0:
            violations.append(Violation(
                violation_type="dead_code_candidate",
                severity="low",
                entity_id=nid,
                entity_name=name,
                description=(
                    f"Function '{name}' has no incoming CALLS edges. "
                    "It may be unused (dead code) or only called externally/dynamically."
                ),
                recommendation=(
                    "Verify that this function is not called via dynamic dispatch, "
                    "reflection, or external clients. If truly unused, remove it."
                ),
            ))

    return violations
