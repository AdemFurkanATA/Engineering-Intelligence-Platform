"""
src/goal-service/main.py

Goal Service — Phase 3 MVP  (Phase 3.1: PostgreSQL persistence)

Provides the Goal API: engineers submit natural-language engineering goals;
the service classifies them, builds a rule-based plan, executes the plan
against Phase 2 analysis endpoints, and returns a structured engineering report.

Lifecycle:
    submitted → planning → executing → completed | failed | cancelled

Endpoints:
    POST   /goals                  Submit a new goal
    GET    /goals                  List all goals (newest first)
    GET    /goals/{goal_id}        Full goal record (plan + report)
    DELETE /goals/{goal_id}        Cancel a running goal
    GET    /goals/{goal_id}/report Report only
    GET    /health                 Health check

Persistence (Phase 3.1):
    DATABASE_URL env var → PostgreSQLGoalStore (asyncpg + JSONB)
    Not set              → InMemoryGoalStore   (zero-config, default)
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

sys.path.insert(0, os.path.dirname(__file__))

from executor import execute_plan
from models import (
    Goal,
    GoalReport,
    GoalRequest,
    GoalStatus,
    GoalSummary,
    GoalType,
    Severity,
)
from persistence import GoalStore, create_store
from planner import build_plan, classify
from report_builder import build_report

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Service URL config
# ---------------------------------------------------------------------------

SERVICE_URLS: Dict[str, str] = {
    "graph-service":        os.getenv("GRAPH_SERVICE_URL",        "http://localhost:8005"),
    "git-analyzer-service": os.getenv("GIT_ANALYZER_SERVICE_URL", "http://localhost:8008"),
}

# ---------------------------------------------------------------------------
# Module-level state (initialised in lifespan)
# ---------------------------------------------------------------------------

_store: GoalStore                      # populated by lifespan
_tasks: Dict[str, asyncio.Task] = {}   # goal_id → background asyncio.Task


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _register_task(goal_id: str, task: asyncio.Task) -> None:
    """Track an asyncio.Task and clean up _tasks on completion."""
    _tasks[goal_id] = task

    def _cleanup(t: asyncio.Task) -> None:
        _tasks.pop(goal_id, None)

    task.add_done_callback(_cleanup)


# ---------------------------------------------------------------------------
# Background goal execution
# ---------------------------------------------------------------------------

async def _execute_goal(goal: Goal) -> None:
    """Run in the background.  Mutates goal.status, goal.plan, goal.report,
    and persists each transition to the store."""

    # --- Planning phase ---
    goal.status    = GoalStatus.PLANNING
    goal.goal_type = classify(goal.goal_text)
    await _store.save(goal)

    repo_id = goal.repository_id or goal.organization_id or "unknown"
    plan = build_plan(
        goal.goal_type,
        repo_id,
        entity_id    = goal.entity_id,
        entity_type  = goal.entity_type,
        change_scope = goal.change_scope,
    )
    goal.plan = plan
    logger.info(
        "Goal %s classified as '%s' — %d steps for repo '%s'",
        goal.goal_id[:8], goal.goal_type.value, len(plan.steps), repo_id,
    )

    # --- Execution phase ---
    goal.status     = GoalStatus.EXECUTING
    goal.started_at = _now_iso()
    start_ts = time.monotonic()
    await _store.save(goal)

    success, error_msg = await execute_plan(plan, SERVICE_URLS)

    # --- Report building ---
    report = build_report(goal, plan, start_ts)
    goal.report = report

    if success:
        goal.status       = GoalStatus.COMPLETED
        goal.completed_at = _now_iso()
        logger.info(
            "Goal %s completed — severity=%s exec_ms=%s",
            goal.goal_id[:8], report.severity.value, report.execution_ms,
        )
    else:
        goal.status       = GoalStatus.FAILED
        goal.error        = error_msg
        goal.completed_at = _now_iso()
        logger.warning("Goal %s FAILED: %s", goal.goal_id[:8], error_msg)

    await _store.save(goal)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _store
    _store = await create_store()
    logger.info(
        "goal-service starting — backend=%s graph=%s git-analyzer=%s",
        _store.backend,
        SERVICE_URLS["graph-service"],
        SERVICE_URLS["git-analyzer-service"],
    )
    yield
    # Cancel any running goal tasks on shutdown
    for task in list(_tasks.values()):
        task.cancel()
    await _store.close()
    logger.info("goal-service stopped")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Goal Service",
    description=(
        "Phase 3 Goal API — submit engineering goals and receive structured "
        "reports produced by orchestrating Phase 2 analysis endpoints.\n\n"
        "Phase 3.2: Retry (3x backoff), parallel steps, idempotency key."
    ),
    version="3.2.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health", tags=["Operations"])
async def health():
    return {
        "status":  "ok",
        "service": "goal-service",
        "version": "3.2.0",
        "backend": _store.backend,
        "running": len(_tasks),
        "services": SERVICE_URLS,
    }


# ---------------------------------------------------------------------------
# POST /goals — Submit a goal
# ---------------------------------------------------------------------------

@app.post("/goals", status_code=202, tags=["Goals"])
async def submit_goal(req: GoalRequest) -> dict:
    """Submit a natural-language engineering goal.

    The service classifies the goal, builds a rule-based execution plan, and
    runs it asynchronously against Phase 2 analysis endpoints.

    Supported goal types (auto-detected from keywords):
    - **risk_analysis** — "risk", "riskli", "bottleneck", "critical"
    - **impact_analysis** — "impact", "etkilenir", "değişiklik", "blast radius"
    - **architecture_report** — "mimari", "pattern", "violation", "ihlal"
    - **documentation_gaps** — "documentation", "ADR", "decision memory", "eksik"

    Optionally scope the analysis:
    - **entityType** / **entityId** — focus on a specific node (for impact analysis)
    - **changeScope** — describe the planned change for context
    - **idempotencyKey** — if a non-failed goal with this key already exists,
      the server returns it (HTTP 200) instead of creating a duplicate.

    Returns a goal record with `goalId`.  Poll `GET /goals/{goalId}` for status.
    """
    # --- Idempotency check ---
    if req.idempotency_key:
        existing = await _store.find_by_idempotency_key(req.idempotency_key)
        if existing and existing.status not in (GoalStatus.FAILED, GoalStatus.CANCELLED):
            logger.info(
                "Idempotency hit for key=%s → goal %s",
                req.idempotency_key, existing.goal_id[:8],
            )
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=200,
                content={
                    "goalId":      existing.goal_id,
                    "status":      existing.status.value,
                    "submittedAt": existing.submitted_at,
                    "message":     "Existing goal returned (idempotency key match).",
                    "idempotent":  True,
                },
            )

    goal = Goal(
        goal_text        = req.goal,
        repository_id    = req.repository_id,
        organization_id  = req.organization_id,
        entity_type      = req.entity_type,
        entity_id        = req.entity_id,
        change_scope     = req.change_scope,
        idempotency_key  = req.idempotency_key,
    )
    await _store.save(goal)

    task = asyncio.create_task(_execute_goal(goal))
    _register_task(goal.goal_id, task)

    logger.info("Goal submitted: %s — '%s'", goal.goal_id[:8], goal.goal_text[:60])
    return {
        "goalId":      goal.goal_id,
        "status":      goal.status.value,
        "submittedAt": goal.submitted_at,
        "message":     "Goal accepted and queued for execution.",
    }


# ---------------------------------------------------------------------------
# GET /goals — List goals
# ---------------------------------------------------------------------------

@app.get("/goals", tags=["Goals"])
async def list_goals(
    status:        Optional[str] = Query(None, description="Filter by status"),
    goal_type:     Optional[str] = Query(None, alias="goalType"),
    repository_id: Optional[str] = Query(None, alias="repositoryId"),
    limit:         int           = Query(50,   ge=1, le=200),
) -> dict:
    """List all goals, newest first.

    Supports optional filtering by status, goalType, or repositoryId.
    """
    goals = await _store.list_all(
        status        = status,
        goal_type     = goal_type,
        repository_id = repository_id,
        limit         = limit,
    )

    summaries = [
        GoalSummary(
            goal_id       = g.goal_id,
            goal_text     = g.goal_text,
            goal_type     = g.goal_type,
            status        = g.status,
            severity      = g.report.severity if g.report else None,
            submitted_at  = g.submitted_at,
            completed_at  = g.completed_at,
            repository_id = g.repository_id,
        )
        for g in goals
    ]

    return {"data": [s.model_dump() for s in summaries], "total": len(summaries)}


# ---------------------------------------------------------------------------
# GET /goals/{goal_id} — Full goal record
# ---------------------------------------------------------------------------

@app.get("/goals/{goal_id}", tags=["Goals"])
async def get_goal(goal_id: str) -> dict:
    """Return the full goal record, including plan step results and report."""
    goal = await _store.get(goal_id)
    if not goal:
        raise HTTPException(status_code=404, detail=f"Goal '{goal_id}' not found.")
    return goal.model_dump()


# ---------------------------------------------------------------------------
# GET /goals/{goal_id}/report — Report only
# ---------------------------------------------------------------------------

@app.get("/goals/{goal_id}/report", tags=["Goals"])
async def get_report(goal_id: str) -> dict:
    """Return only the engineering report for a completed or failed goal.

    Status codes:
    - **200** — goal completed *or* failed; report is always returned so the
      caller can inspect findings, failed step details, and the error message.
      A failed goal (e.g. required step returned 404 because the repository is
      not yet synced) still produces a structured report with ``goal.error`` set.
    - **404** — goal ID not found.
    - **409** — goal is still in progress (submitted / planning / executing /
      cancelled before a report was generated).  Poll and retry.
    """
    goal = await _store.get(goal_id)
    if not goal:
        raise HTTPException(status_code=404, detail=f"Goal '{goal_id}' not found.")
    if goal.status not in (GoalStatus.COMPLETED, GoalStatus.FAILED):
        raise HTTPException(
            status_code=409,
            detail=f"Goal '{goal_id}' is still {goal.status.value}. Report not ready.",
        )
    if not goal.report:
        raise HTTPException(status_code=404, detail="No report generated for this goal.")
    return goal.report.model_dump()


# ---------------------------------------------------------------------------
# DELETE /goals/{goal_id} — Cancel a goal
# ---------------------------------------------------------------------------

@app.delete("/goals/{goal_id}", tags=["Goals"])
async def cancel_goal(goal_id: str) -> dict:
    """Cancel a running goal.

    Returns the previous status in the response.
    Has no effect if the goal is already completed or failed.
    """
    goal = await _store.get(goal_id)
    if not goal:
        raise HTTPException(status_code=404, detail=f"Goal '{goal_id}' not found.")

    previous_status = goal.status.value

    task = _tasks.get(goal_id)
    if task and not task.done():
        task.cancel()
        goal.status       = GoalStatus.CANCELLED
        goal.completed_at = _now_iso()
        await _store.save(goal)
        logger.info("Goal %s cancelled (was: %s)", goal_id[:8], previous_status)
    else:
        logger.info("Goal %s cancel requested but already %s", goal_id[:8], previous_status)

    return {
        "goalId":         goal_id,
        "cancelled":      goal.status == GoalStatus.CANCELLED,
        "previousStatus": previous_status,
        "currentStatus":  goal.status.value,
    }
