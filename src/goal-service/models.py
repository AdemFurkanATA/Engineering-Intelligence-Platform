"""
src/goal-service/models.py

Pydantic models for the goal-service.

Lifecycle:
    submitted → planning → executing → completed | failed | cancelled
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class GoalStatus(str, Enum):
    SUBMITTED  = "submitted"
    PLANNING   = "planning"
    EXECUTING  = "executing"
    COMPLETED  = "completed"
    FAILED     = "failed"
    CANCELLED  = "cancelled"


class GoalType(str, Enum):
    RISK_ANALYSIS        = "risk_analysis"
    IMPACT_ANALYSIS      = "impact_analysis"
    ARCHITECTURE_REPORT  = "architecture_report"
    DOCUMENTATION_GAPS   = "documentation_gaps"
    UNKNOWN              = "unknown"


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH     = "high"
    MEDIUM   = "medium"
    LOW      = "low"
    NONE     = "none"


class StepStatus(str, Enum):
    PENDING   = "pending"
    RUNNING   = "running"
    COMPLETED = "completed"
    FAILED    = "failed"
    SKIPPED   = "skipped"


# ---------------------------------------------------------------------------
# Plan / Step
# ---------------------------------------------------------------------------

class PlanStep(BaseModel):
    """A single executable step in a goal plan."""
    name:        str
    description: str
    endpoint:    str            # path relative to service base, e.g. /graph/analysis/patterns/{repo_id}
    service:     str            # "graph-service" | "git-analyzer-service"
    params:      Dict[str, Any] = Field(default_factory=dict)
    required:    bool           = True
    status:      StepStatus     = StepStatus.PENDING
    result:      Optional[Any]  = None
    error:       Optional[str]  = None
    duration_ms: Optional[int]  = None


class Plan(BaseModel):
    """Ordered list of steps to execute for a goal."""
    goal_type:   GoalType
    steps:       List[PlanStep]
    created_at:  str = Field(default_factory=_now_iso)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

class Finding(BaseModel):
    """A single finding within an engineering report."""
    category:    str
    title:       str
    description: str
    severity:    Severity
    evidence:    List[str] = Field(default_factory=list)
    node_id:     Optional[str] = None


class Recommendation(BaseModel):
    """An actionable recommendation based on findings."""
    title:       str
    description: str
    priority:    Severity
    action:      str          # short imperative, e.g. "Refactor X", "Add test coverage"


class GoalReport(BaseModel):
    """Structured engineering report produced after executing a plan."""
    goal_id:         str
    goal_type:       GoalType
    repository_id:   Optional[str]
    organization_id: Optional[str]
    summary:         str
    severity:        Severity
    findings:        List[Finding]        = Field(default_factory=list)
    recommendations: List[Recommendation] = Field(default_factory=list)
    data_points:     Dict[str, Any]       = Field(default_factory=dict)
    generated_at:    str                  = Field(default_factory=_now_iso)
    execution_ms:    Optional[int]        = None


# ---------------------------------------------------------------------------
# Goal
# ---------------------------------------------------------------------------

class GoalRequest(BaseModel):
    """Input payload for POST /goals.

    Fields
    ------
    goal            : Natural-language engineering goal (required).
    repository_id   : Target repository node ID.
    organization_id : Organization ID for cross-repo goals.
    entity_type     : Optional entity focus — "service" | "module" | "class" | "function".
                      Used by impact_analysis to scope the blast-radius calculation.
    entity_id       : Optional graph node ID of the specific entity to focus on.
    change_scope    : Optional free-text description of the planned change.
                      E.g. "Remove the PaymentService.processRefund method".
                      Included in the plan as context for impact analysis.
    """
    model_config = ConfigDict(populate_by_name=True)

    goal:            str  = Field(..., min_length=5, description="Natural-language engineering goal")
    repository_id:   Optional[str] = Field(None, alias="repositoryId")
    organization_id: Optional[str] = Field(None, alias="organizationId")
    entity_type:     Optional[str] = Field(None, alias="entityType",
                                           description="service | module | class | function")
    entity_id:       Optional[str] = Field(None, alias="entityId",
                                           description="Graph node ID of the target entity")
    change_scope:    Optional[str] = Field(None, alias="changeScope",
                                           description="Description of the planned change")


class Goal(BaseModel):
    """Full internal goal record."""
    goal_id:         str        = Field(default_factory=_new_id)
    goal_text:       str
    goal_type:       GoalType   = GoalType.UNKNOWN
    status:          GoalStatus = GoalStatus.SUBMITTED
    repository_id:   Optional[str] = None
    organization_id: Optional[str] = None
    # Entity targeting (Phase 3.0+)
    entity_type:     Optional[str] = None   # service | module | class | function
    entity_id:       Optional[str] = None   # specific graph node ID to focus on
    change_scope:    Optional[str] = None   # description of planned change
    plan:            Optional[Plan]       = None
    report:          Optional[GoalReport] = None
    error:           Optional[str]        = None
    submitted_at:    str = Field(default_factory=_now_iso)
    started_at:      Optional[str] = None
    completed_at:    Optional[str] = None


class GoalSummary(BaseModel):
    """Lightweight goal record for list responses."""
    goal_id:       str
    goal_text:     str
    goal_type:     GoalType
    status:        GoalStatus
    severity:      Optional[Severity] = None
    submitted_at:  str
    completed_at:  Optional[str] = None
    repository_id: Optional[str] = None
