"""
src/goal-service/planner.py

Rule-based goal classifier and plan builder.

The planner:
  1. Classifies a natural-language goal into one of 4 GoalTypes.
  2. Builds an ordered Plan (list of PlanSteps) for that goal type.

No LLM is used here — classification is keyword-based.  This is intentional:
the Phase 3 MVP must be reliable and deterministic.  An LLM-backed planner is
a Phase 3.2 upgrade that can replace _classify() without changing anything else.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

from models import GoalType, Plan, PlanStep


# ---------------------------------------------------------------------------
# Keyword sets per goal type.
# Each entry is (GoalType, exact_keywords, prefix_stems).
# - exact_keywords : matched as whole-word (with \b boundaries) — short root forms
# - prefix_stems   : matched as substrings (word starts-with stem) — handles suffixes
#   e.g. stem "risk" matches "riskiest", "risky", "risks"
#        stem "architect" matches "architectural", "architecturally", "architecture"
# Order matters: first match wins.
_PATTERNS: list[tuple[GoalType, list[str], list[str]]] = [
    (
        GoalType.RISK_ANALYSIS,
        # exact word forms (Turkish + English roots)
        ["risk", "risky", "riskli", "riski", "unstable", "kararsız",
         "dangerous", "tehlikeli", "vulnerable", "kritik", "critical",
         "bottleneck", "darboğaz", "instabil"],
        # prefix stems — matches words *starting with* these
        ["risk", "bottleneck", "vulnerab", "critical", "unstab", "instab"],
    ),
    (
        GoalType.IMPACT_ANALYSIS,
        ["impact", "etki", "etkisi", "affect", "etkilenir", "etkilenen",
         "blast", "downstream", "bağımlı", "bağımlılık", "ripple"],
        # "change" → changes, changed; "affect" → affected, affects
        ["impact", "affect", "change", "etkileni", "depend", "downstream"],
    ),
    (
        GoalType.ARCHITECTURE_REPORT,
        ["mimari", "pattern", "desen", "yapı", "violation", "ihlal",
         "coupling", "bağlaşım", "layered", "microservice", "hexagonal"],
        # "architect" → architecture, architectural; "violat" → violation, violations
        ["architect", "violat", "pattern", "coupling", "struct", "hexag",
         "microserv", "layered"],
    ),
    (
        GoalType.DOCUMENTATION_GAPS,
        ["documentation", "dokümantasyon", "doküman", "decision", "karar",
         "memory", "hafıza", "adr", "missing", "eksik", "eksiklik",
         "gap", "boşluk", "undocumented", "belgelenmemiş"],
        ["document", "decision", "doküman", "memory", "hafıza", "missing",
         "eksik", "undocument"],
    ),
]


def _word_tokens(text: str) -> list[str]:
    """Split text into lowercase word tokens (letters only)."""
    return re.findall(r"[a-z\u00c0-\u024f]+", text.lower())


def classify(goal_text: str) -> GoalType:
    """Return the GoalType that best matches the goal text.

    Two-pass matching (case-insensitive):
    1. Exact whole-word match against ``exact_keywords``.
    2. Prefix/stem match: any token that *starts with* a stem in ``prefix_stems``.

    First pattern that matches (in definition order) wins.
    Returns GoalType.UNKNOWN if no pattern matches.
    """
    normalized = goal_text.lower()
    tokens = _word_tokens(goal_text)

    for goal_type, exact_kws, prefix_stems in _PATTERNS:
        # Pass 1: whole-word exact match
        if any(re.search(rf"\b{re.escape(kw)}\b", normalized) for kw in exact_kws):
            return goal_type
        # Pass 2: prefix/stem substring match on tokens
        if any(tok.startswith(stem) for tok in tokens for stem in prefix_stems):
            return goal_type

    return GoalType.UNKNOWN


# ---------------------------------------------------------------------------
# Step templates per goal type
# ---------------------------------------------------------------------------

def _risk_steps(repo_id: str) -> List[PlanStep]:
    return [
        PlanStep(
            name="violations",
            description="Detect architectural violations (god-classes, cycles, orphans)",
            service="graph-service",
            endpoint=f"/graph/analysis/violations/{repo_id}",
            required=True,
            parallel_group="risk-core",
        ),
        PlanStep(
            name="dependency_metrics",
            description="Compute coupling, instability, and bottleneck scores",
            service="graph-service",
            endpoint=f"/graph/analysis/dependency-metrics/{repo_id}",
            required=True,
            parallel_group="risk-core",
        ),
        PlanStep(
            name="pattern",
            description="Detect architectural pattern for context",
            service="graph-service",
            endpoint=f"/graph/analysis/patterns/{repo_id}",
            required=False,
            parallel_group="risk-enrich",
        ),
        PlanStep(
            name="timeline",
            description="Fetch commit timeline to identify churn hotspots",
            service="graph-service",
            endpoint=f"/graph/timeline/{repo_id}",
            params={"limit": 20, "event_type": "commit"},
            required=False,
            parallel_group="risk-enrich",
        ),
    ]


def _impact_steps(repo_id: str) -> List[PlanStep]:
    return [
        PlanStep(
            name="dependency_metrics",
            description="Compute coupling and dependency risk",
            service="graph-service",
            endpoint=f"/graph/analysis/dependency-metrics/{repo_id}",
            required=True,
            parallel_group="impact-core",
        ),
        PlanStep(
            name="violations",
            description="Identify structural violations that amplify blast radius",
            service="graph-service",
            endpoint=f"/graph/analysis/violations/{repo_id}",
            required=True,
            parallel_group="impact-core",
        ),
        PlanStep(
            name="architecture",
            description="Get architecture metrics (function/class counts, top-called)",
            service="graph-service",
            endpoint=f"/graph/analysis/architecture/{repo_id}",
            required=False,
            parallel_group="impact-enrich",
        ),
        PlanStep(
            name="timeline",
            description="Recent change history",
            service="graph-service",
            endpoint=f"/graph/timeline/{repo_id}",
            params={"limit": 10, "event_type": "all"},
            required=False,
            parallel_group="impact-enrich",
        ),
    ]


def _architecture_steps(repo_id: str) -> List[PlanStep]:
    return [
        PlanStep(
            name="pattern",
            description="Detect dominant architectural pattern",
            service="graph-service",
            endpoint=f"/graph/analysis/patterns/{repo_id}",
            required=True,
            parallel_group="arch-core",
        ),
        PlanStep(
            name="violations",
            description="Detect architectural violations",
            service="graph-service",
            endpoint=f"/graph/analysis/violations/{repo_id}",
            required=True,
            parallel_group="arch-core",
        ),
        PlanStep(
            name="architecture",
            description="Compute architecture health metrics",
            service="graph-service",
            endpoint=f"/graph/analysis/architecture/{repo_id}",
            required=True,
            parallel_group="arch-core",
        ),
        PlanStep(
            name="dependency_metrics",
            description="Coupling and instability analysis",
            service="graph-service",
            endpoint=f"/graph/analysis/dependency-metrics/{repo_id}",
            required=False,
            parallel_group="arch-core",
        ),
    ]


def _documentation_steps(repo_id: str) -> List[PlanStep]:
    return [
        PlanStep(
            name="decisions",
            description="Retrieve decision memory (ADRs) for the repository",
            service="graph-service",
            endpoint=f"/decisions/{repo_id}",
            required=True,
        ),
        PlanStep(
            name="repository_node",
            description="Get repository node metadata",
            service="graph-service",
            endpoint=f"/graph/nodes/{repo_id}",
            required=False,
        ),
        PlanStep(
            name="timeline",
            description="Recent commit history for documentation coverage heuristic",
            service="graph-service",
            endpoint=f"/graph/timeline/{repo_id}",
            params={"limit": 20, "event_type": "commit"},
            required=False,
        ),
    ]


_STEP_BUILDERS: Dict[GoalType, object] = {
    GoalType.RISK_ANALYSIS:       _risk_steps,
    GoalType.IMPACT_ANALYSIS:     _impact_steps,
    GoalType.ARCHITECTURE_REPORT: _architecture_steps,
    GoalType.DOCUMENTATION_GAPS:  _documentation_steps,
}


# ---------------------------------------------------------------------------
# Default fallback plan (UNKNOWN goal type)
# ---------------------------------------------------------------------------

def _unknown_steps(repo_id: str) -> List[PlanStep]:
    """Best-effort plan when the goal type is unrecognised."""
    return [
        PlanStep(
            name="pattern",
            description="Detect architectural pattern",
            service="graph-service",
            endpoint=f"/graph/analysis/patterns/{repo_id}",
            required=False,
        ),
        PlanStep(
            name="violations",
            description="Detect architectural violations",
            service="graph-service",
            endpoint=f"/graph/analysis/violations/{repo_id}",
            required=False,
        ),
        PlanStep(
            name="dependency_metrics",
            description="Compute dependency metrics",
            service="graph-service",
            endpoint=f"/graph/analysis/dependency-metrics/{repo_id}",
            required=False,
        ),
    ]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Maps user-facing entityType values to graph-service timeline path segments.
# Falls back to "function" (finest-grained) for unknown types.
_ENTITY_TYPE_PATH: dict[str, str] = {
    "function":  "function",
    "class":     "class",
    "module":    "module",
    "service":   "service",
    "component": "component",
}


def build_plan(
    goal_type:    GoalType,
    repo_id:      str,
    entity_id:    Optional[str] = None,
    entity_type:  Optional[str] = None,
    change_scope: Optional[str] = None,
) -> Plan:
    """Build an ordered Plan for the given goal type and repository.

    Parameters
    ----------
    goal_type    : Classified goal type.
    repo_id      : Target repository node ID.
    entity_id    : Optional graph node ID of a specific entity (e.g. a service
                   or class).  When provided for impact_analysis, an entity
                   timeline step is inserted to scope the blast-radius analysis.
    entity_type  : Entity category — "function" | "class" | "module" | "service".
                   Controls which Phase 2 timeline path segment is used.
                   Defaults to "function" if entity_id is provided but type is omitted.
    change_scope : Optional plain-text description of the planned change.
                   Stored in the step description for the report builder.

    Returns
    -------
    Plan with ordered PlanStep list.
    """
    builder = _STEP_BUILDERS.get(goal_type, _unknown_steps)
    steps: List[PlanStep] = builder(repo_id)  # type: ignore[operator]

    # Entity-scoped enrichment for impact analysis
    if goal_type == GoalType.IMPACT_ANALYSIS and entity_id:
        path_segment = _ENTITY_TYPE_PATH.get((entity_type or "").lower(), "function")
        entity_step = PlanStep(
            name="entity_timeline",
            description=(
                f"Fetch analytical timeline for {path_segment} '{entity_id}'"
                + (f" — change scope: {change_scope[:100]}" if change_scope else "")
            ),
            service="graph-service",
            endpoint=f"/graph/timeline/{path_segment}/{entity_id}",
            # Store entity_type so report_builder can use it in Finding labels
            params={"_entity_type": path_segment, "_entity_id": entity_id},
            required=False,
        )
        # Insert after dependency_metrics (index 1) for logical ordering
        steps.insert(2, entity_step)

    return Plan(goal_type=goal_type, steps=steps)
