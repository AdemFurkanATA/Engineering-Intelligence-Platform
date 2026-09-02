# Phase 2 Release Notes

> **This document is a short release summary. For the full technical handoff, accepted limitations, bug-fix log, and Phase 3 hand-off notes, see [`PHASE_2_COMPLETION.md`](PHASE_2_COMPLETION.md).**

**Release date:** 2026-08-19  
**Test coverage:** 530 passed, 0 failures  
**Closing commit:** Phase 2 MVP stabilization

## What shipped in Phase 2

| Capability | Details |
|---|---|
| Git analysis pipeline | Clone → analyze → event publish (commit, dependency, AST, ADR) |
| Dependency parsers | Python, Node.js, Go, Java, Rust |
| AST symbol extraction | Python + JavaScript (functions, classes, CALLS/IMPLEMENTS) |
| Knowledge Graph nodes | Repository, Commit, Developer, Dependency, Function, Class, Decision |
| Analysis endpoints | patterns, violations, dependency-metrics, architecture, impact, timeline |
| Decision memory | ADR ingestion + `/decisions/{repo_id}` |
| Incremental sync model | Clone-then-filter (`last_sha` tracking) |
| E2E smoke test | 11 in-process assertions + `scripts/ops_smoke.sh` for Docker env |
| Runtime bugs fixed | `@subscriber.on()`, DELETE /jobs status, `_tasks` leak, Windows clone crash |

## Known limitations accepted for MVP

See [`PHASE_2_COMPLETION.md`](PHASE_2_COMPLETION.md#accepted-limitations-carried-to-backlog).

## What comes next

Phase 3 — Goal API + Planner + Report Engine.  
See [`PHASE_3_PLAN.md`](PHASE_3_PLAN.md) for full scope, API contracts, and milestones.
