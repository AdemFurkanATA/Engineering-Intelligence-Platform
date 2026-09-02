"""
pattern_detector.py — Architectural pattern detection for the Engineering Intelligence Platform.

Detects the dominant architectural pattern in a repository based on graph structure:

Patterns detected (rule-based, deterministic):
  - Microservices : ≥3 independent Service nodes + Kafka PRODUCES/CONSUMES relationships
  - Layered       : Node names contain recognisable layer keywords
                    (controller, service/services, repository/repo, model, domain, etc.)
  - Hexagonal     : Directory/module names contain ports, adapters, domain
  - Modular Monolith : Single Service + ≥2 Module nodes
  - Monolith      : Single Service/no clear module separation
  - Unknown       : Not enough information to classify

Each detected pattern carries a confidence score (0.0–1.0) and evidence list.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class PatternEvidence:
    rule:        str
    description: str
    entities:    list[str] = field(default_factory=list)


@dataclass
class PatternResult:
    pattern:     str
    confidence:  float
    evidence:    list[PatternEvidence] = field(default_factory=list)
    description: str = ""

    def to_dict(self) -> dict:
        return {
            "pattern":     self.pattern,
            "confidence":  round(self.confidence, 3),
            "description": self.description,
            "evidence": [
                {
                    "rule":        e.rule,
                    "description": e.description,
                    "entities":    e.entities,
                }
                for e in self.evidence
            ],
        }


# ---------------------------------------------------------------------------
# Layer keyword sets
# ---------------------------------------------------------------------------

_LAYER_KEYWORDS = {
    "controller":  {"controller", "controllers", "handler", "handlers", "resource", "resources",
                    "api", "endpoint", "endpoints", "route", "routes", "view", "views"},
    "service":     {"service", "services", "usecase", "usecases", "application",
                    "business", "logic", "interactor"},
    "repository":  {"repository", "repositories", "repo", "repos", "storage", "store",
                    "dao", "persistence", "data"},
    "model":       {"model", "models", "entity", "entities", "domain", "schema",
                    "schemas", "dto", "dtos"},
}

_HEXAGONAL_KEYWORDS = {
    "ports":    {"port", "ports"},
    "adapters": {"adapter", "adapters", "infrastructure", "infra"},
    "domain":   {"domain", "core"},
}


# ---------------------------------------------------------------------------
# Pattern detection logic
# ---------------------------------------------------------------------------

def detect_patterns(graph_data: dict) -> PatternResult:
    """Detect the dominant architectural pattern from in-memory graph data.

    Args:
        graph_data: Dict with keys:
            - nodes: dict[node_id, {label, properties}]
            - relationships: list[{sourceId, targetId, type}]

    Returns:
        PatternResult with the best-fit pattern and confidence.
    """
    nodes         = graph_data.get("nodes", {})
    relationships = graph_data.get("relationships", [])

    # Group nodes by label
    by_label: dict[str, list[str]] = {}
    for nid, node in nodes.items():
        label = node.get("label", "")
        by_label.setdefault(label, []).append(nid)

    service_nodes  = by_label.get("Service", [])
    module_nodes   = by_label.get("Module", [])
    kafka_nodes    = by_label.get("KafkaTopic", [])
    function_nodes = by_label.get("Function", [])
    class_nodes    = by_label.get("Class", [])

    # Collect all names for keyword matching
    all_names: list[str] = []
    for nid, node in nodes.items():
        props = node.get("properties", {})
        name = (props.get("name") or props.get("title") or nid or "").lower()
        all_names.append(name)

    # Rel type sets
    rel_types = {r.get("type", "") for r in relationships}
    kafka_rels = [r for r in relationships
                  if r.get("type") in ("PRODUCES", "CONSUMES")]

    # ── Candidate scores ────────────────────────────────────────────────────
    candidates: list[tuple[float, PatternResult]] = []

    # Microservices
    score, evidence = _score_microservices(
        service_nodes, kafka_nodes, kafka_rels
    )
    candidates.append((score, PatternResult(
        pattern="Microservices",
        confidence=score,
        evidence=evidence,
        description="Multiple independent services communicating via message broker.",
    )))

    # Layered
    score, evidence = _score_layered(all_names)
    candidates.append((score, PatternResult(
        pattern="Layered",
        confidence=score,
        evidence=evidence,
        description="Codebase organised into horizontal responsibility layers "
                    "(controller → service → repository → model).",
    )))

    # Hexagonal
    score, evidence = _score_hexagonal(all_names)
    candidates.append((score, PatternResult(
        pattern="Hexagonal",
        confidence=score,
        evidence=evidence,
        description="Ports-and-adapters architecture separating domain from infrastructure.",
    )))

    # Modular Monolith
    score, evidence = _score_modular_monolith(service_nodes, module_nodes)
    candidates.append((score, PatternResult(
        pattern="ModularMonolith",
        confidence=score,
        evidence=evidence,
        description="Single deployable unit with explicit internal module boundaries.",
    )))

    # Pick best
    candidates.sort(key=lambda t: t[0], reverse=True)
    best_score, best = candidates[0]

    if best_score < 0.2:
        return PatternResult(
            pattern="Unknown",
            confidence=best_score,
            description="Not enough structural information to classify the architecture.",
        )

    return best


# ---------------------------------------------------------------------------
# Individual scorers
# ---------------------------------------------------------------------------

def _score_microservices(
    services: list[str],
    kafka_topics: list[str],
    kafka_rels: list[dict],
) -> tuple[float, list[PatternEvidence]]:
    evidence = []
    score = 0.0

    if len(services) >= 3:
        score += 0.45
        evidence.append(PatternEvidence(
            rule="multiple_services",
            description=f"{len(services)} independent Service nodes found.",
            entities=services[:10],
        ))
    elif len(services) >= 2:
        score += 0.20

    if len(kafka_topics) >= 1:
        score += 0.25
        evidence.append(PatternEvidence(
            rule="kafka_topics",
            description=f"{len(kafka_topics)} Kafka topic(s) detected (async messaging).",
            entities=kafka_topics[:10],
        ))

    if len(kafka_rels) >= 2:
        score += 0.30
        evidence.append(PatternEvidence(
            rule="kafka_produce_consume",
            description=f"{len(kafka_rels)} PRODUCES/CONSUMES relationships "
                        "indicate event-driven inter-service communication.",
        ))

    return min(score, 1.0), evidence


def _score_layered(names: list[str]) -> tuple[float, list[PatternEvidence]]:
    evidence  = []
    hit_count = 0

    for layer, keywords in _LAYER_KEYWORDS.items():
        matched = [n for n in names if any(kw in n for kw in keywords)]
        if matched:
            hit_count += 1
            evidence.append(PatternEvidence(
                rule=f"layer_{layer}",
                description=f"Layer '{layer}' identified ({len(matched)} matches).",
                entities=matched[:5],
            ))

    # Score: 0.25 per layer hit (4 layers max → 1.0)
    score = hit_count * 0.25
    return min(score, 1.0), evidence


def _score_hexagonal(names: list[str]) -> tuple[float, list[PatternEvidence]]:
    evidence  = []
    hit_count = 0

    for zone, keywords in _HEXAGONAL_KEYWORDS.items():
        matched = [n for n in names if any(kw in n for kw in keywords)]
        if matched:
            hit_count += 1
            evidence.append(PatternEvidence(
                rule=f"hexagonal_{zone}",
                description=f"Hexagonal zone '{zone}' identified ({len(matched)} matches).",
                entities=matched[:5],
            ))

    # Score: 0.33 per zone (3 zones → 1.0)
    score = hit_count * 0.33
    return min(score, 1.0), evidence


def _score_modular_monolith(
    services: list[str],
    modules: list[str],
) -> tuple[float, list[PatternEvidence]]:
    evidence = []
    score    = 0.0

    if len(services) == 1:
        score += 0.35
        evidence.append(PatternEvidence(
            rule="single_service",
            description="Single Service node — likely a monolithic deployable.",
            entities=services,
        ))

    if len(modules) >= 2:
        score += 0.50
        evidence.append(PatternEvidence(
            rule="multiple_modules",
            description=f"{len(modules)} Module nodes provide internal boundaries.",
            entities=modules[:10],
        ))
    elif len(modules) == 1:
        score += 0.15

    return min(score, 1.0), evidence
