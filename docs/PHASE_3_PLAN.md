# Phase 3 Plan — Autonomous Engineering Intelligence

**Status:** 🔵 In Progress — Phase 3.0 Stabilization  
**Started:** 2026-08-20  
**Preceding phase:** [Phase 2 Completion](PHASE_2_COMPLETION.md)

---

## Objective

> "The user submits an engineering goal; the platform decomposes it into analysis steps, calls existing Phase 2 analysis endpoints, and returns a structured engineering report."

No LLM or autonomous agents in Phase 3.0–3.3. Intelligence comes from orchestrating deterministic Phase 2 endpoints and applying domain-specific scoring logic.

---

## Success Criteria

Phase 3 is complete when:

- [ ] `POST /goals` accepts a natural-language goal and returns a structured report
- [ ] All 4 goal types classified correctly with Turkish + English input
- [ ] Reports contain findings, severity scores, and actionable recommendations
- [ ] Goals persisted across service restarts (Phase 3.1)
- [ ] `GET /goals/{id}/report` returns 409 (not 200 with empty data) for missing repos
- [ ] `docker compose config -q` passes (infra only, app services run via `start_all.ps1`)
- [ ] Full test suite ≥ 565 passed, 0 failures

---

## Scope

### In Scope (Phase 3.0–3.3)

| Item | Milestone |
|---|---|
| Goal API (`POST /goals`, `GET /goals`, `GET /goals/{id}`, `DELETE /goals/{id}`) | 3.0 ✅ |
| Rule-based Planner (4 goal types + UNKNOWN fallback) | 3.0 ✅ |
| Plan Executor (async HTTP orchestration of Phase 2 endpoints) | 3.0 ✅ |
| Report Builder (findings, severity, recommendations per goal type) | 3.0 ✅ |
| Classifier: Turkish + English keyword + prefix-stem matching | 3.0 ✅ |
| Entity targeting (`entityType`, `entityId`, `changeScope` fields) | 3.0 ✅ |
| Proper 404/no-data model (required step 404 → goal failed with message) | 3.0 ✅ |
| docker-compose.yml valid (infra-only, no app service cross-deps) | 3.0 ✅ |
| PostgreSQL goal/plan/report persistence | 3.1 ✅ |
| Retry logic on transient HTTP errors (3× with backoff) | 3.2 |
| Parallel execution of independent plan steps | 3.2 |
| Idempotency key support | 3.2 |
| User/organization authorization and audit trail | 3.2 |
| Finding deduplication and recommendation ranking | 3.3 |
| Evidence links (clickable graph node references) | 3.3 |
| Confidence scores on findings | 3.3 |

### Out of Scope (explicitly deferred)

| Item | Reason |
|---|---|
| LLM-backed planner | Phase 3.4 — needs reliable orchestration first |
| Autonomous agents | Phase 3.5+ — needs goal persistence and audit trail |
| Direct code modification | Never — only human-approved suggestions |
| PR/issue creation | Phase 3.6 — requires GitHub/GitLab connector |
| Scheduled scans | Phase 3.5 — needs persistent goal store first |

---

## API Contracts

### POST /goals

```http
POST /goals
Content-Type: application/json

{
  "goal":           "Bu repository'de en riskli modülleri bul.",
  "repositoryId":   "my-service",
  "organizationId": "org-001",
  "entityType":     "service",
  "entityId":       "PaymentService",
  "changeScope":    "Remove the processRefund method"
}
```

**Response 202:**
```json
{
  "goalId":      "abc-123",
  "status":      "submitted",
  "submittedAt": "2026-09-01T18:00:00Z",
  "message":     "Goal accepted and queued for execution."
}
```

### GET /goals/{goalId}/report

**Response 200 (completed):**
```json
{
  "goal_id":         "abc-123",
  "goal_type":       "risk_analysis",
  "repository_id":   "my-service",
  "organization_id": "org-001",
  "summary":         "Risk analysis complete. 3 violations detected...",
  "severity":        "high",
  "findings": [
    {
      "category":    "architectural_violation",
      "title":       "God Class",
      "description": "Too many methods",
      "severity":    "high",
      "evidence":    ["PaymentService"],
      "node_id":     "PaymentService"
    }
  ],
  "recommendations": [
    {
      "title":       "Resolve architectural violations",
      "description": "3 violation(s) detected...",
      "priority":    "high",
      "action":      "Refactor violating nodes"
    }
  ],
  "data_points":    { ... },
  "generated_at":   "2026-09-01T18:00:05Z",
  "execution_ms":   342
}
```

**Response 409 (not yet complete):**
**Error responses:**
- `409 Conflict` — goal still in progress (submitted/planning/executing); report not ready. Poll and retry.
- `404 Not Found` — goal ID not found.
- `200 OK` with failed goal — required step returned 404 (repo not synced). The response body contains the structured report with `goal.error` explaining which step failed and why (e.g. "Required step 'violations' returned 404 — repository/entity not found in the knowledge graph. Ensure the repository has been synced via POST /analyze first.").

### DELETE /goals/{goalId}

```json
{
  "goalId":         "abc-123",
  "cancelled":      true,
  "previousStatus": "executing",
  "currentStatus":  "cancelled"
}
```

---

## Goal Types & Classification

| Goal Type | English stems | Turkish stems |
|---|---|---|
| `risk_analysis` | risk*, bottleneck*, vulnerab*, critical*, unstab* | risk*, riskli, kararsız, tehlikeli, instabil |
| `impact_analysis` | impact*, affect*, change*, depend*, downstream* | etki*, etkileni*, bağımlı, bağımlılık |
| `architecture_report` | architect*, violat*, pattern*, coupling*, struct*, hexag*, microserv* | mimari, ihlal, yapı, desen, bağlaşım |
| `documentation_gaps` | document*, decision*, memory*, missing*, undocument* | doküman*, karar*, hafıza*, eksik*, belgelenmemiş |

`*` = prefix-stem match (handles suffixed forms).

---

## Security Boundaries

- goal-service makes **outbound HTTP GET calls only** to internal services
- No code execution, no file system writes, no git operations
- No external network calls
- Authorization/RBAC deferred to Phase 3.2

---

## Service Architecture

```
goal-service (:8009)
  ↓ HTTP GET
graph-service (:8005)      — violations, patterns, dependency-metrics, architecture, timeline
git-analyzer-service (:8008) — job status (read-only for Phase 3.0)
```

`docker-compose.yml` contains **infrastructure only** (postgres, redis, kafka, neo4j, qdrant, zookeeper).  
Application services start via `start_all.ps1` (local dev) or individual `uvicorn` commands.

---

## Testing Strategy

| Layer | File | Coverage |
|---|---|---|
| Classifier | `tests/test_goal_service.py::TestClassifier` | 10 cases (EN + TR, variants) |
| Plan builder | `tests/test_goal_service.py::TestPlanBuilder` | 6 cases (all goal types, entity_id) |
| Report builder | `tests/test_goal_service.py::TestReportBuilder` | 8 cases (mock data per goal type) |
| API endpoints | `tests/test_goal_service.py::TestGoalAPI` | 11 cases (CRUD + filter + 409/404) |

Full suite: **565 passed, 0 failures**.
