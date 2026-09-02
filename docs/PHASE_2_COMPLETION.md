# Phase 2 — Engineering Intelligence: Completion Checklist

**Status:** ✅ MVP Complete  
**Date:** 2026-08-19  
**Tests:** 530 passed, 0 failures

This document is the official hand-off record for Phase 2.
It lists every committed capability, accepted limitations, and open backlog items
carried forward into Phase 3.

---

## Delivered Capabilities

### Git Analysis Pipeline

- [x] Repository clone via GitPython (`POST /analyze`)
- [x] Shallow clone with configurable depth (`GIT_MAX_COMMITS`)
- [x] Async job queue with semaphore concurrency control
- [x] Job lifecycle: `queued → running → cloning → analyzing → completed | failed`
- [x] Job cancellation with correct `previousStatus` in response (`DELETE /jobs/{id}`)
- [x] Task registry memory-leak prevention (`_register_task` + `add_done_callback`)
- [x] Retry logic with exponential backoff (`GIT_MAX_RETRIES`)
- [x] Windows-compatible clone (no `kill_after_timeout` on Windows)

### Event Pipeline

- [x] `repository.cloned` — RepositoryCloned event with commit count and size
- [x] `repository.clone_failed` — RepositoryCloneFailed event with error detail
- [x] `commit.analyzed` — CommitAnalyzed event per commit (author, SHA, files changed)
- [x] `dependency.detected` — DependencyDetected event per dependency
- [x] `architecture.analyzed` — ArchitectureAnalyzed event (AST symbols, relations)
- [x] `decision.recorded` — DecisionRecorded event (ADR files, commit references)

### Dependency Parsers

- [x] Python `requirements.txt`
- [x] Node.js `package.json`
- [x] Go `go.mod`
- [x] Java `pom.xml`
- [x] Rust `Cargo.toml`

### AST Symbol Extraction

- [x] Python parser — functions, classes, CALLS/IMPLEMENTS relations
- [x] JavaScript parser — functions, classes
- [x] Language-agnostic fallback

### Knowledge Graph (graph-service)

- [x] Repository node creation/update on RepositoryCloned
- [x] Commit node creation on CommitAnalyzed
- [x] Developer node creation + COMMITTED_BY relationship
- [x] Dependency node creation + DEPENDS_ON relationship
- [x] Function/Class node creation + CALLS/IMPLEMENTS relationships
- [x] Decision node creation + RECORDED_FOR relationship
- [x] `lastAnalyzedSha` tracking for incremental sync

### Analysis Endpoints

- [x] `GET /graph/analysis/patterns/{repo_id}` — architectural pattern detection
- [x] `GET /graph/analysis/violations/{repo_id}` — violation detection (god-class, cycles, orphans, etc.)
- [x] `GET /graph/analysis/dependency-metrics/{repo_id}` — coupling, instability, risk report
- [x] `GET /graph/analysis/architecture/{repo_id}` — function/class counts, top-called, language breakdown
- [x] `GET /graph/timeline/{repo_id}` — raw commit + dependency event feed
- [x] `GET /graph/timeline/{entity_type}/{entity_id}` — analytical churn/frequency metrics
- [x] `GET /graph/repos/{repo_id}/last_sha` — incremental sync state
- [x] `GET /graph/analysis/impact/{repo_id}` — change impact analysis

### Decision Memory

- [x] `DecisionRecorded` event handler in graph-service
- [x] Decision node stored in graph with RECORDED_FOR → Repository
- [x] `GET /decisions/{repo_id}` — list decisions for a repository

### Job Lifecycle API

- [x] `POST /analyze` — submit sync job
- [x] `GET /jobs` — list all jobs
- [x] `GET /jobs/{job_id}` — get job status
- [x] `DELETE /jobs/{job_id}` — cancel job with correct previousStatus

### Infrastructure

- [x] `docker-compose.yml` — git-analyzer-service containerised
- [x] `start_all.ps1` — local dev startup includes git-analyzer-service
- [x] API Gateway routing for `/analyze`, `/jobs`
- [x] In-memory fallback for Neo4j in all graph endpoints

---

## Test Coverage

| Suite | Count |
|---|---|
| Unit tests | 519 |
| E2E integration smoke | 11 |
| **Total** | **530 passed** |

### E2E Smoke Test (`tests/test_e2e_sync_smoke.py`)

Exercises the full in-process pipeline against a real bare git repo:

- [x] RepositoryCloned event published with commitCount >= 1
- [x] CommitAnalyzed events (>= 3 commits)
- [x] DependencyDetected events (fastapi + requests from requirements.txt)
- [x] Graph Repository node created
- [x] Graph Commit nodes created (>= 3)
- [x] Graph Dependency nodes created
- [x] Incremental since_sha filtering (fewer commits on second run)
- [x] POST /analyze queues job and returns jobId
- [x] DELETE /jobs returns correct previousStatus
- [x] graph-service /health returns status: ok, backend: in-memory
- [x] graph-service /graph/nodes returns list

---

## Accepted Limitations (Carried to Backlog)

| Item | Decision |
|---|---|
| **Incremental sync = clone-then-filter** | MVP-acceptable. Persistent fetch/cache is Phase 3 optimisation for large repos. |
| **E2E test = in-process only** | Real Kafka/Neo4j/Docker validated via `scripts/ops_smoke.sh` (manual run). |
| **AST = Python + JS only** | Sufficient for MVP. Java, Go, Rust parsers deferred to Phase 3. |
| **Decision memory = file-based ADRs** | PR/issue integrations deferred to Phase 3 with GitHub/GitLab connector. |
| **Impact analysis = graph-based only** | LLM-augmented impact scoring deferred to Phase 3. |

---

## Runtime Bugs Fixed

| Bug | Fix Location |
|---|---|
| `@subscriber.on()` AttributeError at import | `src/graph-service/main.py:64`, `main.py:565` |
| `DELETE /jobs` returned post-mutation status | `src/git-analyzer-service/main.py:645` |
| `_tasks` dict memory leak | `src/git-analyzer-service/main.py:132` |
| `kill_after_timeout` crash on Windows | `src/git-analyzer-service/main.py:233` |

---

## Hand-off to Phase 3

Phase 3 starts with the **Goal API + Planner + Report Engine** (`goal-service`, port `:8009`).

The goal-service orchestrates existing Phase 2 endpoints to answer engineering questions:

- "Bu repository'de en riskli modüller hangileri?"
- "Bu serviste değişiklik yaparsam ne etkilenir?"
- "Mimari ihlalleri ve önerileri raporla."
- "Decision memory / dokümantasyon eksiklerini çıkar."

See [`docs/phases.md`](phases.md) for the full Phase 3 roadmap.
