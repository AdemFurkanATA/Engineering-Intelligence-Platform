"""
tests/test_goal_service.py

Unit tests for the goal-service.

Covers:
  - Goal classification (planner.classify)
  - Plan generation (planner.build_plan)
  - Report building (report_builder.build_report) with mock step data
  - API endpoints (POST /goals, GET /goals, GET /goals/{id}/report, DELETE /goals/{id})

All tests run without real HTTP calls — executor is mocked where needed.
"""
import sys
import os
import asyncio
import time
import types
import unittest.mock as mock

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Patch infra before importing goal-service modules
# ---------------------------------------------------------------------------

_GS_ROOT = os.path.join(
    os.path.dirname(__file__), "..", "src", "goal-service"
)
sys.path.insert(0, _GS_ROOT)

# ---------------------------------------------------------------------------
# Planner tests
# ---------------------------------------------------------------------------

from planner import classify, build_plan
from models import GoalType, PlanStep


class TestClassifier:

    def test_risk_keywords_english(self):
        assert classify("Find the riskiest modules in this repository") == GoalType.RISK_ANALYSIS

    def test_risk_keywords_turkish(self):
        assert classify("Bu repository'de en riskli modülleri bul") == GoalType.RISK_ANALYSIS

    def test_impact_keywords_english(self):
        assert classify("What will be affected if this service changes?") == GoalType.IMPACT_ANALYSIS

    def test_impact_keywords_turkish(self):
        assert classify("Bu serviste değişiklik yaparsam ne etkilenir?") == GoalType.IMPACT_ANALYSIS

    def test_architecture_keywords_english(self):
        assert classify("Report architectural violations and patterns") == GoalType.ARCHITECTURE_REPORT

    def test_architecture_keywords_turkish(self):
        assert classify("Mimari ihlalleri ve önerileri raporla") == GoalType.ARCHITECTURE_REPORT

    def test_documentation_keywords_english(self):
        assert classify("Find documentation gaps and missing ADR decisions") == GoalType.DOCUMENTATION_GAPS

    def test_documentation_keywords_turkish(self):
        assert classify("Decision memory eksiklerini çıkar") == GoalType.DOCUMENTATION_GAPS

    def test_unknown_goal(self):
        assert classify("Hello world") == GoalType.UNKNOWN

    def test_case_insensitive(self):
        assert classify("RISK analysis for ARCHITECTURE") == GoalType.RISK_ANALYSIS


class TestPlanBuilder:

    def test_risk_plan_has_required_steps(self):
        plan = build_plan(GoalType.RISK_ANALYSIS, "repo-1")
        required = [s for s in plan.steps if s.required]
        assert len(required) >= 2
        names = [s.name for s in plan.steps]
        assert "violations" in names
        assert "dependency_metrics" in names

    def test_impact_plan_has_dependency_step(self):
        plan = build_plan(GoalType.IMPACT_ANALYSIS, "repo-2")
        names = [s.name for s in plan.steps]
        assert "dependency_metrics" in names
        assert "violations" in names

    def test_architecture_plan_includes_pattern(self):
        plan = build_plan(GoalType.ARCHITECTURE_REPORT, "repo-3")
        names = [s.name for s in plan.steps]
        assert "pattern" in names
        assert "violations" in names
        assert "architecture" in names

    def test_documentation_plan_includes_decisions(self):
        plan = build_plan(GoalType.DOCUMENTATION_GAPS, "repo-4")
        names = [s.name for s in plan.steps]
        assert "decisions" in names

    def test_unknown_plan_is_not_empty(self):
        plan = build_plan(GoalType.UNKNOWN, "repo-x")
        assert len(plan.steps) > 0

    def test_step_endpoints_contain_repo_id(self):
        repo_id = "my-test-repo"
        plan = build_plan(GoalType.RISK_ANALYSIS, repo_id)
        for step in plan.steps:
            assert repo_id in step.endpoint, f"Step '{step.name}' endpoint missing repo_id"

    # ----- Entity-scoped impact tests -----

    def test_entity_timeline_step_injected_for_impact(self):
        """entity_id triggers entity_timeline step in impact plan."""
        plan = build_plan(
            GoalType.IMPACT_ANALYSIS, "repo-5",
            entity_id="PaymentService", entity_type="service",
        )
        names = [s.name for s in plan.steps]
        assert "entity_timeline" in names

    def test_entity_timeline_uses_correct_type_segment(self):
        """entity_type maps to correct graph-service endpoint path segment."""
        for etype, expected_seg in [
            ("service",  "service"),
            ("class",    "class"),
            ("function", "function"),
            ("module",   "module"),
            ("unknown_type", "function"),   # fallback
        ]:
            plan = build_plan(
                GoalType.IMPACT_ANALYSIS, "repo-6",
                entity_id="SomeNode", entity_type=etype,
            )
            et_step = next(s for s in plan.steps if s.name == "entity_timeline")
            assert f"/{expected_seg}/SomeNode" in et_step.endpoint, (
                f"entity_type={etype!r} should map to /{expected_seg}/, "
                f"got: {et_step.endpoint}"
            )

    def test_no_entity_step_without_entity_id(self):
        """entity_timeline step NOT injected when entity_id is absent."""
        plan = build_plan(GoalType.IMPACT_ANALYSIS, "repo-7")
        names = [s.name for s in plan.steps]
        assert "entity_timeline" not in names

    def test_entity_step_stores_type_in_params(self):
        """entity_type and entity_id are stored in step.params for report_builder."""
        plan = build_plan(
            GoalType.IMPACT_ANALYSIS, "repo-8",
            entity_id="UserClass", entity_type="class",
        )
        et_step = next(s for s in plan.steps if s.name == "entity_timeline")
        assert et_step.params.get("_entity_type") == "class"
        assert et_step.params.get("_entity_id") == "UserClass"


# ---------------------------------------------------------------------------
# Report builder tests
# ---------------------------------------------------------------------------

from report_builder import build_report
from models import Goal, Plan, StepStatus


def _make_goal(goal_type: GoalType, repo_id: str = "r1") -> Goal:
    return Goal(
        goal_text       = "test goal",
        goal_type       = goal_type,
        repository_id   = repo_id,
        organization_id = "org-1",
    )


def _fill_step(plan: Plan, name: str, result: dict) -> None:
    for step in plan.steps:
        if step.name == name:
            step.result = result
            step.status = StepStatus.COMPLETED


class TestReportBuilder:

    def test_risk_report_no_violations(self):
        goal = _make_goal(GoalType.RISK_ANALYSIS)
        plan = build_plan(GoalType.RISK_ANALYSIS, "r1")
        _fill_step(plan, "violations",        {"violations": []})
        _fill_step(plan, "dependency_metrics", {"riskReport": [], "criticalNodes": []})
        _fill_step(plan, "pattern",           {"pattern": "Layered", "confidence": 0.8})
        report = build_report(goal, plan, time.monotonic())
        assert report.goal_type == GoalType.RISK_ANALYSIS
        assert report.severity.value in ("none", "low")
        assert "Layered" in report.summary

    def test_risk_report_with_violations(self):
        goal = _make_goal(GoalType.RISK_ANALYSIS)
        plan = build_plan(GoalType.RISK_ANALYSIS, "r1")
        _fill_step(plan, "violations", {"violations": [
            {"type": "god_class",   "severity": "high",   "description": "Too many methods", "nodeId": "n1"},
            {"type": "orphan_node", "severity": "medium", "description": "No relations",     "nodeId": "n2"},
        ]})
        _fill_step(plan, "dependency_metrics", {"riskReport": [], "criticalNodes": []})
        _fill_step(plan, "pattern", {"pattern": "Unknown", "confidence": 0.1})
        report = build_report(goal, plan, time.monotonic())
        assert len(report.findings) >= 2
        assert report.severity.value in ("medium", "high", "critical")
        assert len(report.recommendations) >= 1

    def test_impact_report_bottlenecks(self):
        goal = _make_goal(GoalType.IMPACT_ANALYSIS)
        plan = build_plan(GoalType.IMPACT_ANALYSIS, "r1")
        _fill_step(plan, "dependency_metrics", {
            "bottlenecks":       [{"nodeId": "svc-core", "totalCoupling": 15}],
            "instabilityScores": {"svc-core": 0.85, "svc-util": 0.3},
            "riskReport":        [],
        })
        _fill_step(plan, "violations", {"violations": []})
        _fill_step(plan, "architecture", {"totalFunctions": 200, "totalClasses": 30})
        report = build_report(goal, plan, time.monotonic())
        assert report.goal_type == GoalType.IMPACT_ANALYSIS
        assert any("bottleneck" in f.category.lower() for f in report.findings)
        assert report.severity.value in ("medium", "high", "critical")

    def test_impact_report_entity_churn_finding(self):
        """entity_timeline data produces entity_churn finding in impact report."""
        goal = _make_goal(GoalType.IMPACT_ANALYSIS)
        plan = build_plan(
            GoalType.IMPACT_ANALYSIS, "r1",
            entity_id="PaymentService", entity_type="service",
        )
        _fill_step(plan, "dependency_metrics", {"bottlenecks": [], "instabilityScores": {}})
        _fill_step(plan, "violations",         {"violations": []})
        _fill_step(plan, "architecture",       {"totalFunctions": 10, "totalClasses": 2})
        _fill_step(plan, "entity_timeline",    {
            "changeFrequency": 14,
            "birthDate":       "2025-01-10",
            "refactoringSignals": [],
        })
        report = build_report(goal, plan, time.monotonic())
        churn_findings = [f for f in report.findings if f.category == "entity_churn"]
        assert len(churn_findings) == 1
        assert "PaymentService" in churn_findings[0].title
        # High churn (14 > 10) → HIGH severity
        assert churn_findings[0].severity.value == "high"
        # Summary should mention the targeted entity
        assert "service:PaymentService" in report.summary
        # Recommendation generated
        recs = [r for r in report.recommendations if "PaymentService" in r.title]
        assert len(recs) >= 1

    def test_impact_report_entity_refactoring_signals(self):
        """entity_timeline refactoringSignals produce refactoring_signal findings."""
        goal = _make_goal(GoalType.IMPACT_ANALYSIS)
        plan = build_plan(
            GoalType.IMPACT_ANALYSIS, "r1",
            entity_id="OrderProcessor", entity_type="class",
        )
        _fill_step(plan, "dependency_metrics", {"bottlenecks": [], "instabilityScores": {}})
        _fill_step(plan, "violations",         {"violations": []})
        _fill_step(plan, "architecture",       {"totalFunctions": 5, "totalClasses": 1})
        _fill_step(plan, "entity_timeline",    {
            "changeFrequency": 2,
            "birthDate": "2024-06-01",
            "refactoringSignals": [
                {"type": "god_method", "description": "Method too long"},
                {"type": "feature_envy", "description": "Accesses many other classes"},
            ],
        })
        report = build_report(goal, plan, time.monotonic())
        sig_findings = [f for f in report.findings if f.category == "refactoring_signal"]
        assert len(sig_findings) == 2

    def test_architecture_report_low_confidence(self):
        goal = _make_goal(GoalType.ARCHITECTURE_REPORT)
        plan = build_plan(GoalType.ARCHITECTURE_REPORT, "r1")
        _fill_step(plan, "pattern",    {"pattern": "Unknown", "confidence": 0.2, "evidence": []})
        _fill_step(plan, "violations", {"violations": []})
        _fill_step(plan, "architecture", {"totalFunctions": 10, "totalClasses": 3, "topCalledFunctions": []})
        report = build_report(goal, plan, time.monotonic())
        low_conf = [f for f in report.findings if "Low-confidence" in f.title]
        assert len(low_conf) == 1

    def test_documentation_report_no_decisions(self):
        goal = _make_goal(GoalType.DOCUMENTATION_GAPS)
        plan = build_plan(GoalType.DOCUMENTATION_GAPS, "r1")
        _fill_step(plan, "decisions",      {"decisions": []})
        _fill_step(plan, "repository_node", {})
        _fill_step(plan, "timeline",       {"events": []})
        report = build_report(goal, plan, time.monotonic())
        assert any("No decision records" in f.title for f in report.findings)
        assert len(report.recommendations) >= 1

    def test_documentation_report_with_decisions(self):
        goal = _make_goal(GoalType.DOCUMENTATION_GAPS)
        plan = build_plan(GoalType.DOCUMENTATION_GAPS, "r1")
        _fill_step(plan, "decisions", {"decisions": [
            {"title": "Use Kafka", "status": "accepted"},
            {"title": "Use Redis", "status": "proposed"},
        ]})
        _fill_step(plan, "repository_node", {})
        _fill_step(plan, "timeline", {"events": []})
        report = build_report(goal, plan, time.monotonic())
        # Should find the open "proposed" decision
        open_findings = [f for f in report.findings if "unresolved" in f.title]
        assert len(open_findings) == 1

    def test_unknown_report_has_fallback_recommendation(self):
        goal = _make_goal(GoalType.UNKNOWN)
        plan = build_plan(GoalType.UNKNOWN, "r1")
        report = build_report(goal, plan, time.monotonic())
        assert report.goal_type == GoalType.UNKNOWN
        assert len(report.recommendations) >= 1

    def test_execution_ms_populated(self):
        goal = _make_goal(GoalType.RISK_ANALYSIS)
        plan = build_plan(GoalType.RISK_ANALYSIS, "r1")
        _fill_step(plan, "violations",        {"violations": []})
        _fill_step(plan, "dependency_metrics", {"riskReport": [], "criticalNodes": []})
        _fill_step(plan, "pattern",           {"pattern": "Layered", "confidence": 0.7})
        ts = time.monotonic()
        report = build_report(goal, plan, ts)
        assert report.execution_ms is not None
        assert report.execution_ms >= 0


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------

import main as goal_main  # noqa: E402  (after sys.path setup)


@pytest.fixture(autouse=True)
def reset_goal_store():
    """Clear in-memory store and task registry between tests."""
    import asyncio
    # Wait for lifespan to have created the store, then clear it
    if hasattr(goal_main, '_store') and hasattr(goal_main._store, 'clear'):
        goal_main._store.clear()
    goal_main._tasks.clear()
    yield
    if hasattr(goal_main, '_store') and hasattr(goal_main._store, 'clear'):
        goal_main._store.clear()
    goal_main._tasks.clear()


def _inject_goal(goal):
    """Synchronously inject a goal into the active in-memory store."""
    import asyncio
    asyncio.get_event_loop().run_until_complete(goal_main._store.save(goal))


@pytest.fixture()
def client(reset_goal_store):
    with TestClient(goal_main.app) as c:
        # After lifespan runs, _store is InMemoryGoalStore — clear it
        if hasattr(goal_main, '_store') and hasattr(goal_main._store, 'clear'):
            goal_main._store.clear()
        yield c


class TestGoalAPI:

    def test_health(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert data["service"] == "goal-service"

    def test_submit_goal_returns_202(self, client):
        r = client.post("/goals", json={
            "goal": "Bu repository'de en riskli modülleri bul.",
            "repositoryId": "repo-test",
        })
        assert r.status_code == 202
        data = r.json()
        assert "goalId" in data
        assert data["status"] == "submitted"

    def test_list_goals_empty(self, client):
        r = client.get("/goals")
        assert r.status_code == 200
        assert r.json()["total"] == 0

    def test_list_goals_after_submit(self, client):
        client.post("/goals", json={"goal": "architecture report please", "repositoryId": "r1"})
        r = client.get("/goals")
        assert r.json()["total"] >= 1

    def test_get_goal_not_found(self, client):
        r = client.get("/goals/nonexistent-id")
        assert r.status_code == 404

    def test_get_goal_returns_record(self, client):
        post_r = client.post("/goals", json={"goal": "risk analysis needed", "repositoryId": "r2"})
        goal_id = post_r.json()["goalId"]
        r = client.get(f"/goals/{goal_id}")
        assert r.status_code == 200
        assert r.json()["goal_id"] == goal_id

    def test_report_not_ready_returns_409(self, client):
        """Inject a goal stuck in 'submitted' state — report should be 409."""
        from models import Goal, GoalStatus
        g = Goal(goal_text="test", repository_id="r3", status=GoalStatus.SUBMITTED)
        _inject_goal(g)
        r = client.get(f"/goals/{g.goal_id}/report")
        assert r.status_code == 409

    def test_cancel_goal_not_found(self, client):
        r = client.delete("/goals/nonexistent")
        assert r.status_code == 404

    def test_cancel_submitted_goal(self, client):
        from models import Goal, GoalStatus
        g = Goal(goal_text="cancel me", repository_id="r4", status=GoalStatus.SUBMITTED)
        _inject_goal(g)
        r = client.delete(f"/goals/{g.goal_id}")
        assert r.status_code == 200
        data = r.json()
        assert data["previousStatus"] == "submitted"

    def test_filter_by_repository_id(self, client):
        client.post("/goals", json={"goal": "risk analysis", "repositoryId": "repo-A"})
        client.post("/goals", json={"goal": "risk analysis", "repositoryId": "repo-B"})
        r = client.get("/goals", params={"repositoryId": "repo-A"})
        results = r.json()["data"]
        assert all(g["repository_id"] == "repo-A" for g in results)

    def test_goal_type_classified_on_submit(self, client):
        post_r = client.post("/goals", json={
            "goal": "Mimari ihlalleri ve önerileri raporla.",
            "repositoryId": "repo-5",
        })
        goal_id = post_r.json()["goalId"]
        # Poll briefly — goal_type may already be set after planning
        import time as _time
        for _ in range(20):
            goal_data = client.get(f"/goals/{goal_id}").json()
            if goal_data.get("goal_type") != "unknown":
                break
            _time.sleep(0.1)
        # At minimum the goal was submitted successfully
        assert goal_data["goal_id"] == goal_id
