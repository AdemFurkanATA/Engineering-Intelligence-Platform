"""
dependency_metrics.py — Coupling, instability and risk analysis for the Engineering Intelligence Platform.

Computes the following metrics per module/package node:

  instability(M) = outDegree(M) / (inDegree(M) + outDegree(M))
                   Range [0, 1]:  0 = maximally stable, 1 = maximally unstable

  coupling(M) = inDegree(M) + outDegree(M)
                Total number of dependency relationships.

  risk = coupling score weighted by instability:
         high-coupling AND high-instability = highest risk

Outputs:
  - per-node instability and coupling scores
  - critical nodes    (top N by in-degree — things many depend on)
  - bottlenecks       (nodes that bridge many dependency paths)
  - risk report       (nodes exceeding configurable thresholds)
  - package graph     (flattened edge list for visualisation)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

DEFAULT_CRITICAL_NODE_MIN_IN_DEGREE   = 3
DEFAULT_HIGH_COUPLING_THRESHOLD       = 10
DEFAULT_HIGH_INSTABILITY_THRESHOLD    = 0.7
DEFAULT_TOP_N                         = 10


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class NodeMetrics:
    node_id:     str
    name:        str
    label:       str
    in_degree:   int
    out_degree:  int
    coupling:    int
    instability: float   # 0.0–1.0

    def to_dict(self) -> dict:
        return {
            "nodeId":      self.node_id,
            "name":        self.name,
            "label":       self.label,
            "inDegree":    self.in_degree,
            "outDegree":   self.out_degree,
            "coupling":    self.coupling,
            "instability": round(self.instability, 3),
        }


@dataclass
class DependencyReport:
    package_graph:      list[dict]          = field(default_factory=list)
    node_metrics:       list[NodeMetrics]   = field(default_factory=list)
    critical_nodes:     list[NodeMetrics]   = field(default_factory=list)
    bottlenecks:        list[NodeMetrics]   = field(default_factory=list)
    instability_scores: dict[str, float]    = field(default_factory=dict)
    risk_report:        list[dict]          = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "packageGraph":      self.package_graph,
            "nodeMetrics":       [m.to_dict() for m in self.node_metrics],
            "criticalNodes":     [m.to_dict() for m in self.critical_nodes],
            "bottlenecks":       [m.to_dict() for m in self.bottlenecks],
            "instabilityScores": {k: round(v, 3) for k, v in self.instability_scores.items()},
            "riskReport":        self.risk_report,
        }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def compute_dependency_metrics(
    graph_data: dict,
    critical_in_degree: int = DEFAULT_CRITICAL_NODE_MIN_IN_DEGREE,
    high_coupling:      int = DEFAULT_HIGH_COUPLING_THRESHOLD,
    high_instability:   float = DEFAULT_HIGH_INSTABILITY_THRESHOLD,
    top_n:              int = DEFAULT_TOP_N,
    target_labels:      Optional[set[str]] = None,
) -> DependencyReport:
    """Compute coupling/instability/risk metrics from graph data.

    Args:
        graph_data:         Dict with 'nodes' and 'relationships'.
        critical_in_degree: Minimum in-degree to be flagged as critical.
        high_coupling:      Coupling count above which a node is flagged.
        high_instability:   Instability score above which a node is flagged.
        top_n:              Max number of items in critical_nodes / bottlenecks.
        target_labels:      If set, only analyse nodes with these labels.
                            Defaults to {"Service", "Module", "Class", "Package"}.

    Returns:
        DependencyReport with all computed metrics.
    """
    if target_labels is None:
        target_labels = {"Service", "Module", "Class", "Package", "Function"}

    nodes         = graph_data.get("nodes", {})
    relationships = graph_data.get("relationships", [])

    # Filter to relevant node labels
    relevant = {
        nid: n for nid, n in nodes.items()
        if n.get("label", "") in target_labels
    }

    # Compute degree counts
    in_deg:  dict[str, int] = {nid: 0 for nid in relevant}
    out_deg: dict[str, int] = {nid: 0 for nid in relevant}

    for rel in relationships:
        rel_type = rel.get("type", "")
        if rel_type not in ("DEPENDS_ON", "CALLS", "IMPLEMENTS"):
            continue
        src = rel.get("sourceId", "")
        tgt = rel.get("targetId", "")
        if src in relevant:
            out_deg[src] += 1
        if tgt in relevant:
            in_deg[tgt] += 1

    # Build NodeMetrics objects
    all_metrics: list[NodeMetrics] = []
    for nid, node in relevant.items():
        props = node.get("properties", {})
        name  = props.get("name") or props.get("title") or nid
        label = node.get("label", "")
        ind   = in_deg[nid]
        outd  = out_deg[nid]
        total = ind + outd
        inst  = outd / total if total > 0 else 0.0

        all_metrics.append(NodeMetrics(
            node_id=nid, name=name, label=label,
            in_degree=ind, out_degree=outd,
            coupling=total, instability=inst,
        ))

    # Sort by coupling descending
    all_metrics.sort(key=lambda m: m.coupling, reverse=True)

    # Critical nodes: high in-degree (many dependents — changes ripple widely)
    critical = sorted(
        [m for m in all_metrics if m.in_degree >= critical_in_degree],
        key=lambda m: m.in_degree, reverse=True,
    )[:top_n]

    # Bottlenecks: high total coupling (hub nodes)
    bottlenecks = sorted(all_metrics, key=lambda m: m.coupling, reverse=True)[:top_n]

    # Instability scores dict
    instability_scores = {m.node_id: m.instability for m in all_metrics}

    # Risk report: nodes with BOTH high coupling AND high instability
    risk_report = [
        {
            "nodeId":      m.node_id,
            "name":        m.name,
            "coupling":    m.coupling,
            "instability": round(m.instability, 3),
            "riskLevel":   _risk_level(m.coupling, m.instability,
                                        high_coupling, high_instability),
        }
        for m in all_metrics
        if m.coupling >= high_coupling or m.instability >= high_instability
    ]
    risk_report.sort(key=lambda r: (r["coupling"] + r["instability"] * 10), reverse=True)

    # Package graph (edge list for visualisation)
    dep_types = {"DEPENDS_ON", "CALLS", "IMPLEMENTS"}
    package_graph = [
        {
            "source":     r.get("sourceId"),
            "target":     r.get("targetId"),
            "type":       r.get("type"),
            "sourceName": _get_name(nodes.get(r.get("sourceId", ""), {})),
            "targetName": _get_name(nodes.get(r.get("targetId", ""), {})),
        }
        for r in relationships
        if r.get("type") in dep_types
        and r.get("sourceId") in relevant
        and r.get("targetId") in relevant
    ]

    return DependencyReport(
        package_graph=package_graph,
        node_metrics=all_metrics,
        critical_nodes=critical,
        bottlenecks=bottlenecks,
        instability_scores=instability_scores,
        risk_report=risk_report,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_name(node: dict) -> str:
    props = node.get("properties", {})
    return props.get("name") or props.get("title") or ""


def _risk_level(coupling: int, instability: float,
                high_coupling: int, high_instability: float) -> str:
    if coupling >= high_coupling and instability >= high_instability:
        return "critical"
    if coupling >= high_coupling or instability >= high_instability * 1.2:
        return "high"
    if coupling >= high_coupling // 2:
        return "medium"
    return "low"
