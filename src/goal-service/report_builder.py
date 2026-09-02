"""
src/goal-service/report_builder.py

Converts raw step results from the executor into a structured GoalReport.

Each build_* function handles one GoalType.  They extract findings and
recommendations from the raw API payloads and produce severity scores.

Severity rules:
  - CRITICAL : immediate action required (e.g. 5+ violations, instability > 0.8)
  - HIGH      : significant concern (2–4 violations, instability 0.6–0.8)
  - MEDIUM    : worth addressing (1 violation, instability 0.4–0.6)
  - LOW       : informational, no urgent action needed
  - NONE      : no issues detected
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from models import (
    Finding,
    Goal,
    GoalReport,
    GoalType,
    Plan,
    Recommendation,
    Severity,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _severity_from_count(n: int) -> Severity:
    if n >= 5:  return Severity.CRITICAL
    if n >= 3:  return Severity.HIGH
    if n >= 1:  return Severity.MEDIUM
    return Severity.NONE


def _max_severity(severities: List[Severity]) -> Severity:
    order = [Severity.NONE, Severity.LOW, Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL]
    if not severities:
        return Severity.NONE
    return max(severities, key=lambda s: order.index(s))


def _get(data: Any, *keys: str, default: Any = None) -> Any:
    """Safe nested dict accessor."""
    for key in keys:
        if not isinstance(data, dict):
            return default
        data = data.get(key, default)
    return data


def _step_result(plan: Plan, name: str) -> Dict[str, Any]:
    for step in plan.steps:
        if step.name == name and step.result:
            return step.result if isinstance(step.result, dict) else {}
    return {}


# ---------------------------------------------------------------------------
# Risk Analysis report
# ---------------------------------------------------------------------------

def _build_risk_report(goal: Goal, plan: Plan, start_ts: float) -> GoalReport:
    violations  = _step_result(plan, "violations")
    dep_metrics = _step_result(plan, "dependency_metrics")
    pattern     = _step_result(plan, "pattern")
    timeline    = _step_result(plan, "timeline")

    findings:        List[Finding]        = []
    recommendations: List[Recommendation] = []

    # --- Violations ---
    viol_list = violations.get("violations", [])
    for v in viol_list:
        sev = Severity.HIGH if v.get("severity") in ("critical", "high") else Severity.MEDIUM
        findings.append(Finding(
            category    = "architectural_violation",
            title       = v.get("type", "Unknown Violation"),
            description = v.get("description", ""),
            severity    = sev,
            evidence    = [v.get("nodeId", "")],
            node_id     = v.get("nodeId"),
        ))

    if viol_list:
        recommendations.append(Recommendation(
            title       = "Resolve architectural violations",
            description = f"{len(viol_list)} violation(s) detected. Address god-classes and cyclic dependencies first.",
            priority    = _severity_from_count(len(viol_list)),
            action      = "Refactor violating nodes — prioritize god-classes and cyclic dependencies",
        ))

    # --- Dependency Risk ---
    risk_nodes = dep_metrics.get("riskReport", [])
    for rn in risk_nodes[:3]:
        findings.append(Finding(
            category    = "dependency_risk",
            title       = f"High-risk node: {rn.get('nodeId', '?')}",
            description = f"Coupling={rn.get('totalCoupling', '?')}, Instability={rn.get('instability', '?'):.2f}",
            severity    = Severity.HIGH,
            evidence    = [rn.get("nodeId", "")],
            node_id     = rn.get("nodeId"),
        ))

    critical_nodes = dep_metrics.get("criticalNodes", [])
    if critical_nodes:
        findings.append(Finding(
            category    = "critical_dependency",
            title       = f"{len(critical_nodes)} critical dependency node(s)",
            description = "Nodes with high in-degree that many other components depend on.",
            severity    = _severity_from_count(len(critical_nodes)),
            evidence    = [n.get("nodeId", "") for n in critical_nodes[:5]],
        ))
        recommendations.append(Recommendation(
            title       = "Reduce critical node exposure",
            description = "Extract interfaces or introduce adapters around high in-degree nodes.",
            priority    = Severity.HIGH,
            action      = "Introduce abstraction layer around critical dependency nodes",
        ))

    # --- Pattern context ---
    pattern_name = pattern.get("pattern", "Unknown")
    confidence   = pattern.get("confidence", 0.0)

    overall_severity = _max_severity([f.severity for f in findings])
    summary = (
        f"Risk analysis complete. Detected {len(viol_list)} violation(s) and "
        f"{len(risk_nodes)} high-risk dependency node(s). "
        f"Architectural pattern: {pattern_name} (confidence: {confidence:.0%}). "
        f"Overall risk: {overall_severity.value.upper()}."
    )

    return GoalReport(
        goal_id         = goal.goal_id,
        goal_type       = GoalType.RISK_ANALYSIS,
        repository_id   = goal.repository_id,
        organization_id = goal.organization_id,
        summary         = summary,
        severity        = overall_severity,
        findings        = findings,
        recommendations = recommendations,
        data_points     = {
            "violations":        violations,
            "dependencyMetrics": dep_metrics,
            "pattern":           pattern,
        },
        execution_ms = int((time.monotonic() - start_ts) * 1000),
    )


# ---------------------------------------------------------------------------
# Impact Analysis report
# ---------------------------------------------------------------------------

def _build_impact_report(goal: Goal, plan: Plan, start_ts: float) -> GoalReport:
    dep_metrics      = _step_result(plan, "dependency_metrics")
    violations       = _step_result(plan, "violations")
    architecture     = _step_result(plan, "architecture")
    entity_timeline  = _step_result(plan, "entity_timeline")   # populated when entityId given

    findings:        List[Finding]        = []
    recommendations: List[Recommendation] = []

    # --- Entity-scoped analysis (when entityId was provided) ---
    entity_label = ""
    if entity_timeline:
        # Retrieve entity type/id from the step params stored by the planner
        entity_step = next((s for s in plan.steps if s.name == "entity_timeline"), None)
        entity_type_label = (entity_step.params or {}).get("_entity_type", "entity") if entity_step else "entity"
        entity_id_label   = (entity_step.params or {}).get("_entity_id", "?") if entity_step else "?"
        entity_label      = f"{entity_type_label}:{entity_id_label}"

        churn            = entity_timeline.get("churn", entity_timeline.get("changeFrequency", 0))
        birth_date       = entity_timeline.get("birthDate", "unknown")
        refactor_signals = entity_timeline.get("refactoringSignals", [])

        if churn:
            churn_val = float(churn) if isinstance(churn, (int, float)) else 0.0
            churn_sev = Severity.HIGH if churn_val > 10 else Severity.MEDIUM if churn_val > 5 else Severity.LOW
            findings.append(Finding(
                category    = "entity_churn",
                title       = f"High churn: {entity_type_label} '{entity_id_label}'",
                description = (
                    f"This {entity_type_label} changed {churn_val:.0f} times (churn score). "
                    f"First seen: {birth_date}. High churn amplifies blast radius of modifications."
                ),
                severity    = churn_sev,
                evidence    = [entity_id_label],
                node_id     = entity_id_label,
            ))
            if churn_sev in (Severity.HIGH, Severity.MEDIUM):
                recommendations.append(Recommendation(
                    title       = f"Reduce churn in {entity_type_label} '{entity_id_label}'",
                    description = (
                        f"This {entity_type_label} has changed frequently ({churn_val:.0f}×). "
                        "Consider extracting stable abstractions to isolate change impact."
                    ),
                    priority    = churn_sev,
                    action      = f"Extract stable interface from high-churn {entity_type_label}",
                ))

        for sig in refactor_signals[:3]:
            findings.append(Finding(
                category    = "refactoring_signal",
                title       = f"Refactoring signal: {sig.get('type', 'unknown')}",
                description = sig.get("description", ""),
                severity    = Severity.MEDIUM,
                evidence    = [entity_id_label],
                node_id     = entity_id_label,
            ))

    # --- Repository-level bottlenecks ---
    bottlenecks = dep_metrics.get("bottlenecks", [])
    for b in bottlenecks[:5]:
        findings.append(Finding(
            category    = "bottleneck",
            title       = f"Bottleneck: {b.get('nodeId', '?')}",
            description = f"Total coupling: {b.get('totalCoupling', '?')}. Changes here propagate widely.",
            severity    = Severity.HIGH,
            evidence    = [b.get("nodeId", "")],
            node_id     = b.get("nodeId"),
        ))

    instability_scores = dep_metrics.get("instabilityScores", {})
    highly_unstable = [(nid, s) for nid, s in instability_scores.items() if s > 0.7]
    if highly_unstable:
        findings.append(Finding(
            category    = "instability",
            title       = f"{len(highly_unstable)} highly unstable component(s)",
            description = "Components with instability > 0.7 are change-prone and risky to modify.",
            severity    = _severity_from_count(len(highly_unstable)),
            evidence    = [nid for nid, _ in highly_unstable[:5]],
        ))
        recommendations.append(Recommendation(
            title       = "Stabilize high-instability components",
            description = "Add abstractions or interfaces to shield consumers from frequent changes.",
            priority    = Severity.HIGH,
            action      = "Add interfaces/adapters around highly unstable components",
        ))

    total_fns = architecture.get("totalFunctions", 0)
    if total_fns > 100:
        findings.append(Finding(
            category    = "complexity",
            title       = f"High function count: {total_fns}",
            description = "Large surface area increases blast radius of changes.",
            severity    = Severity.MEDIUM,
            evidence    = [],
        ))

    overall_severity = _max_severity([f.severity for f in findings])
    entity_clause    = f" Targeted {entity_label} analysis included." if entity_label else ""
    summary = (
        f"Impact analysis complete.{entity_clause} "
        f"{len(bottlenecks)} bottleneck(s) detected, "
        f"{len(highly_unstable)} highly unstable component(s). "
        f"Overall change impact risk: {overall_severity.value.upper()}."
    )

    return GoalReport(
        goal_id         = goal.goal_id,
        goal_type       = GoalType.IMPACT_ANALYSIS,
        repository_id   = goal.repository_id,
        organization_id = goal.organization_id,
        summary         = summary,
        severity        = overall_severity,
        findings        = findings,
        recommendations = recommendations,
        data_points     = {
            "dependencyMetrics": dep_metrics,
            "violations":        violations,
            "architecture":      architecture,
        },
        execution_ms = int((time.monotonic() - start_ts) * 1000),
    )


# ---------------------------------------------------------------------------
# Architecture Report
# ---------------------------------------------------------------------------

def _build_architecture_report(goal: Goal, plan: Plan, start_ts: float) -> GoalReport:
    pattern      = _step_result(plan, "pattern")
    violations   = _step_result(plan, "violations")
    architecture = _step_result(plan, "architecture")
    dep_metrics  = _step_result(plan, "dependency_metrics")

    findings:        List[Finding]        = []
    recommendations: List[Recommendation] = []

    # Pattern
    pattern_name = pattern.get("pattern", "Unknown")
    confidence   = pattern.get("confidence", 0.0)
    evidence     = pattern.get("evidence", [])

    if confidence < 0.4:
        findings.append(Finding(
            category    = "pattern",
            title       = f"Low-confidence pattern: {pattern_name}",
            description = f"Pattern '{pattern_name}' detected with only {confidence:.0%} confidence. Architecture may be hybrid or undetermined.",
            severity    = Severity.MEDIUM,
            evidence    = evidence,
        ))
    else:
        findings.append(Finding(
            category    = "pattern",
            title       = f"Detected pattern: {pattern_name}",
            description = f"Pattern '{pattern_name}' detected with {confidence:.0%} confidence.",
            severity    = Severity.NONE,
            evidence    = evidence,
        ))

    # Violations
    viol_list = violations.get("violations", [])
    for v in viol_list:
        sev = Severity.HIGH if v.get("severity") in ("critical", "high") else Severity.MEDIUM
        findings.append(Finding(
            category    = "violation",
            title       = v.get("type", "Violation"),
            description = v.get("description", ""),
            severity    = sev,
            evidence    = [v.get("nodeId", "")],
            node_id     = v.get("nodeId"),
        ))

    if viol_list:
        recommendations.append(Recommendation(
            title       = "Resolve architecture violations",
            description = f"{len(viol_list)} violation(s) undermine the '{pattern_name}' pattern.",
            priority    = _severity_from_count(len(viol_list)),
            action      = "Refactor according to pattern constraints — see violation list",
        ))

    # Architecture metrics
    total_fns   = architecture.get("totalFunctions", 0)
    total_cls   = architecture.get("totalClasses",   0)
    top_called  = architecture.get("topCalledFunctions", [])

    if top_called and top_called[0].get("inDegree", 0) > 10:
        findings.append(Finding(
            category    = "god_function",
            title       = f"Potential god-function: {top_called[0].get('name', '?')}",
            description = f"Called {top_called[0].get('inDegree', 0)} times — likely a central coordination point.",
            severity    = Severity.MEDIUM,
            evidence    = [top_called[0].get("nodeId", "")],
        ))

    overall_severity = _max_severity([f.severity for f in findings if f.severity != Severity.NONE])
    summary = (
        f"Architecture report: pattern='{pattern_name}' ({confidence:.0%} confidence), "
        f"{len(viol_list)} violation(s), {total_fns} functions, {total_cls} classes. "
        f"Overall: {overall_severity.value.upper()}."
    )

    return GoalReport(
        goal_id         = goal.goal_id,
        goal_type       = GoalType.ARCHITECTURE_REPORT,
        repository_id   = goal.repository_id,
        organization_id = goal.organization_id,
        summary         = summary,
        severity        = overall_severity or Severity.LOW,
        findings        = findings,
        recommendations = recommendations,
        data_points     = {
            "pattern":      pattern,
            "violations":   violations,
            "architecture": architecture,
            "depMetrics":   dep_metrics,
        },
        execution_ms = int((time.monotonic() - start_ts) * 1000),
    )


# ---------------------------------------------------------------------------
# Documentation Gaps report
# ---------------------------------------------------------------------------

def _build_documentation_report(goal: Goal, plan: Plan, start_ts: float) -> GoalReport:
    decisions   = _step_result(plan, "decisions")
    repo_node   = _step_result(plan, "repository_node")
    timeline    = _step_result(plan, "timeline")

    findings:        List[Finding]        = []
    recommendations: List[Recommendation] = []

    decision_list = decisions.get("decisions", decisions.get("data", []))
    if not decision_list:
        findings.append(Finding(
            category    = "decision_memory",
            title       = "No decision records found",
            description = "No ADRs or decision records have been ingested for this repository.",
            severity    = Severity.HIGH,
            evidence    = [],
        ))
        recommendations.append(Recommendation(
            title       = "Add Architecture Decision Records (ADRs)",
            description = "Create ADR files in docs/decisions/ and re-sync the repository.",
            priority    = Severity.HIGH,
            action      = "Create docs/decisions/0001-*.md with context, decision, and consequences",
        ))
    else:
        open_decisions = [d for d in decision_list if d.get("status") in ("proposed", "open", "draft")]
        if open_decisions:
            findings.append(Finding(
                category    = "open_decisions",
                title       = f"{len(open_decisions)} unresolved decision(s)",
                description = "These decisions are proposed/open but not yet accepted or superseded.",
                severity    = Severity.MEDIUM,
                evidence    = [d.get("title", "") for d in open_decisions[:3]],
            ))

    # Commit activity vs decision count ratio
    commit_events = [e for e in timeline.get("events", []) if e.get("type") == "commit"]
    if commit_events and len(decision_list) == 0:
        findings.append(Finding(
            category    = "documentation_lag",
            title       = f"{len(commit_events)} commits with no decision records",
            description = "Active development without any documented architectural decisions.",
            severity    = Severity.MEDIUM,
            evidence    = [],
        ))

    overall_severity = _max_severity([f.severity for f in findings])
    summary = (
        f"Documentation gap analysis: {len(decision_list)} decision record(s) found. "
        f"{len(findings)} gap(s) identified. "
        f"Overall: {overall_severity.value.upper()}."
    )

    return GoalReport(
        goal_id         = goal.goal_id,
        goal_type       = GoalType.DOCUMENTATION_GAPS,
        repository_id   = goal.repository_id,
        organization_id = goal.organization_id,
        summary         = summary,
        severity        = overall_severity or Severity.LOW,
        findings        = findings,
        recommendations = recommendations,
        data_points     = {
            "decisions":  decisions,
            "repoNode":   repo_node,
            "timeline":   timeline,
        },
        execution_ms = int((time.monotonic() - start_ts) * 1000),
    )


# ---------------------------------------------------------------------------
# Fallback
# ---------------------------------------------------------------------------

def _build_unknown_report(goal: Goal, plan: Plan, start_ts: float) -> GoalReport:
    completed = [s for s in plan.steps if s.result]
    return GoalReport(
        goal_id         = goal.goal_id,
        goal_type       = GoalType.UNKNOWN,
        repository_id   = goal.repository_id,
        organization_id = goal.organization_id,
        summary         = (
            f"Goal '{goal.goal_text[:80]}' did not match a known template. "
            f"Ran {len(completed)} analysis step(s) and collected raw data."
        ),
        severity        = Severity.LOW,
        findings        = [],
        recommendations = [Recommendation(
            title       = "Refine your goal description",
            description = "Use keywords like: risk, impact, architecture, documentation, violations.",
            priority    = Severity.LOW,
            action      = "Resubmit goal with more specific language",
        )],
        data_points  = {s.name: s.result for s in plan.steps if s.result},
        execution_ms = int((time.monotonic() - start_ts) * 1000),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_BUILDERS = {
    GoalType.RISK_ANALYSIS:       _build_risk_report,
    GoalType.IMPACT_ANALYSIS:     _build_impact_report,
    GoalType.ARCHITECTURE_REPORT: _build_architecture_report,
    GoalType.DOCUMENTATION_GAPS:  _build_documentation_report,
    GoalType.UNKNOWN:             _build_unknown_report,
}


def build_report(goal: Goal, plan: Plan, start_ts: float) -> GoalReport:
    """Assemble a GoalReport from executed plan step results.

    Parameters
    ----------
    goal      : The Goal record (for metadata like repo_id, org_id).
    plan      : Executed plan with step.result populated.
    start_ts  : time.monotonic() timestamp from when execution began.

    Returns
    -------
    GoalReport with findings, recommendations, severity, and summary.
    """
    builder = _BUILDERS.get(plan.goal_type, _build_unknown_report)
    return builder(goal, plan, start_ts)
