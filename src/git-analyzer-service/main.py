"""
git-analyzer-service — FastAPI application.

Phase 2 responsibilities:
- Listen to repository.created and repository.sync_requested events
- Queue and track clone/analysis jobs with full lifecycle management
- Clone the repository to a temporary directory using GitPython
- Detect and parse dependencies (Python/Node/Go/Rust)
- Read commit history (last GIT_MAX_COMMITS commits)
- Publish domain events:
    RepositoryCloned      → repository.cloned
    DependencyDetected    → dependency.detected  (one per dependency)
    CommitAnalyzed        → commit.analyzed      (one per commit)
    ArchitectureAnalyzed  → architecture.analyzed

Job Lifecycle:
    queued → running → succeeded | failed
    Failed jobs are retried up to GIT_MAX_RETRIES times (exponential backoff).

Storage: stateless — all state is in Kafka events and downstream services.
Clones are written to a temp directory and deleted after analysis.

Configuration (env vars):
    GIT_MAX_COMMITS        int   default 100
    GIT_CLONE_TIMEOUT_SEC  int   default 120
    GIT_CLONE_DIR          str   default /tmp/eip-clones
    GIT_MAX_CONCURRENT     int   default 3
    GIT_MAX_RETRIES        int   default 2
    KAFKA_BOOTSTRAP_SERVERS str  default localhost:9092
"""
import asyncio
import logging
import os
import shutil
import sys
import tempfile
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from shared.kafka import EventPublisher, EventSubscriber
from shared.models import (
    CommitAnalyzedPayload,
    DependencyDetectedPayload,
    RepositoryClonedPayload,
    RepositoryCloneFailedPayload,
    ArchitectureAnalyzedPayload,
    CodeSymbol,
    CodeRelation,
    create_event,
)
from parsers import python_parser, node_parser, go_parser, rust_parser, ast_python, ast_javascript

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s"
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
GIT_MAX_COMMITS       = int(os.getenv("GIT_MAX_COMMITS", "100"))
GIT_CLONE_TIMEOUT_SEC = int(os.getenv("GIT_CLONE_TIMEOUT_SEC", "120"))
GIT_CLONE_BASE_DIR    = os.getenv("GIT_CLONE_DIR", tempfile.gettempdir())
GIT_MAX_CONCURRENT    = int(os.getenv("GIT_MAX_CONCURRENT", "3"))
GIT_MAX_RETRIES       = int(os.getenv("GIT_MAX_RETRIES", "2"))
# URL of graph-service for incremental sync last-sha queries
GRAPH_SERVICE_URL     = os.getenv("GRAPH_SERVICE_URL", "http://localhost:8005")

# ---------------------------------------------------------------------------
# Job Lifecycle State Machine
# ---------------------------------------------------------------------------

class JobStatus(str, Enum):
    QUEUED    = "queued"
    RUNNING   = "running"
    SUCCEEDED = "succeeded"
    FAILED    = "failed"


class JobState(BaseModel):
    job_id:      str
    repo_id:     str
    url:         str
    branch:      str
    org_id:      str
    status:      JobStatus = JobStatus.QUEUED
    attempt:     int = 0
    error:       Optional[str] = None
    queued_at:   str
    started_at:  Optional[str] = None
    finished_at: Optional[str] = None
    since_sha:   Optional[str] = None   # incremental sync: only commits after this

    model_config = {"use_enum_values": True}


# In-memory job store (keyed by job_id); last 500 jobs kept
_jobs: dict[str, JobState] = {}
_JOB_MAX = 500

# Task registry: job_id → asyncio.Task (for cancellation)
# Entries are removed automatically via done-callbacks when the task finishes.
_tasks: dict[str, asyncio.Task] = {}

# Semaphore: max concurrent analysis jobs
_job_semaphore: asyncio.Semaphore | None = None   # initialised in lifespan


def _register_task(job_id: str, coro) -> asyncio.Task:
    """Create an asyncio.Task, register it in _tasks, and attach a
    done-callback that automatically removes it from _tasks when it
    completes (success, failure, or cancellation).

    This prevents completed Task objects from accumulating in _tasks
    over the lifetime of the service.
    """
    task = asyncio.create_task(coro)

    def _cleanup(t: asyncio.Task, _jid: str = job_id) -> None:
        _tasks.pop(_jid, None)

    task.add_done_callback(_cleanup)
    _tasks[job_id] = task
    return task


def _trim_jobs():
    """Keep only the most recent _JOB_MAX jobs."""
    if len(_jobs) > _JOB_MAX:
        oldest = sorted(_jobs.keys(),
                        key=lambda jid: _jobs[jid].queued_at)[:len(_jobs) - _JOB_MAX]
        for jid in oldest:
            del _jobs[jid]


def _make_job(repo_id: str, url: str, branch: str, org_id: str,
              since_sha: Optional[str] = None) -> JobState:
    job = JobState(
        job_id=str(uuid.uuid4()),
        repo_id=repo_id,
        url=url,
        branch=branch,
        org_id=org_id,
        queued_at=_now_iso(),
        since_sha=since_sha,
    )
    _jobs[job.job_id] = job
    _trim_jobs()
    return job


# ---------------------------------------------------------------------------
# Kafka
# ---------------------------------------------------------------------------
publisher = EventPublisher()
subscriber = EventSubscriber(
    group_id="git-analyzer-service-group",
    topics=["repository.created", "repository.sync_requested"],
)


# ---------------------------------------------------------------------------
# Core analysis logic
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _fetch_last_sha(repo_id: str) -> Optional[str]:
    """Query graph-service for the last analyzed commit SHA (incremental sync)."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{GRAPH_SERVICE_URL}/graph/repos/{repo_id}/last_sha")
            if r.status_code == 200:
                data = r.json()
                sha = data.get("lastAnalyzedSha")
                if sha:
                    logger.info("Incremental sync: last SHA for %s = %s", repo_id, sha[:8])
                    return sha
    except Exception as exc:
        logger.debug("Could not fetch last SHA for %s: %s", repo_id, exc)
async def _clone_and_analyze(repo_id: str, url: str, org_id: str,
                              branch: str = "main",
                              since_sha: Optional[str] = None) -> None:
    """Clone (or fetch) the repository, run all parsers, publish events.

    If since_sha is provided the service performs an incremental analysis:
    - Uses git fetch instead of a full clone when the clone dir already exists
    - Filters commits to only those after since_sha

    Always cleans up on exit.
    """
    clone_dir = os.path.join(GIT_CLONE_BASE_DIR, f"eip-{repo_id}")
    try:
        import git as gitpython
    except ImportError:
        error_msg = "GitPython not installed — cannot clone repo"
        logger.error("%s %s. Install gitpython in the service container.", error_msg, repo_id)
        await publisher.publish(
            "repository.clone_failed",
            create_event("RepositoryCloneFailed", repo_id, org_id,
                         RepositoryCloneFailedPayload(
                             repositoryId=repo_id, url=url, error=error_msg
                         )),
        )
        return

    # ── Clone (always fresh — clone_dir is deleted after every run) ──────────
    # NOTE: We do not attempt a `git fetch` on an existing clone_dir because
    # the clone dir is unconditionally removed in the `finally` block below.
    # Incremental behaviour is achieved by filtering commits via `since_sha`
    # *after* cloning: only commits newer than the already-processed SHA are
    # re-published as CommitAnalyzed events.  This is safe and correct for
    # shallow clones because we always fetch at least GIT_MAX_COMMITS commits.
    if os.path.exists(clone_dir):
        shutil.rmtree(clone_dir, ignore_errors=True)

    logger.info("Cloning %s (branch=%s, since=%s) → %s",
                url, branch, since_sha or "full", clone_dir)
    try:
        def _do_clone():
            import platform
            kwargs = dict(
                branch=branch,
                depth=GIT_MAX_COMMITS,
                no_single_branch=True,
            )
            # kill_after_timeout relies on UNIX signals — not supported on Windows
            if platform.system() != "Windows":
                kwargs["kill_after_timeout"] = GIT_CLONE_TIMEOUT_SEC
            return gitpython.Repo.clone_from(url, clone_dir, **kwargs)
        repo = await asyncio.to_thread(_do_clone)
    except Exception as exc:
        error_msg = str(exc)[:500]
        logger.error("Clone failed for %s: %s", url, error_msg)
        await publisher.publish(
            "repository.clone_failed",
            create_event("RepositoryCloneFailed", repo_id, org_id,
                         RepositoryCloneFailedPayload(
                             repositoryId=repo_id, url=url, error=error_msg
                         )),
        )
        return

    try:
        root = Path(clone_dir)

        # Count commits — offload stats read (may touch many objects)
        # For incremental sync: filter to commits after since_sha
        def _collect_commits():
            try:
                raw = list(repo.iter_commits(max_count=GIT_MAX_COMMITS))
            except Exception:
                return []
            result = []
            for c in raw:
                # Incremental: stop at the already-processed SHA
                if since_sha and c.hexsha == since_sha:
                    break
                try:
                    result.append({
                        "sha":          c.hexsha,
                        "message":      (c.message or "").strip()[:500],
                        "author_email": c.author.email or "",
                        "author_name":  c.author.name or "",
                        "committed_at": datetime.fromtimestamp(
                                            c.committed_date, tz=timezone.utc),
                        "files_changed": list(c.stats.files.keys())[:50],
                    })
                except Exception as e:
                    logger.warning("Skipping commit %s: %s", getattr(c, "hexsha", "?"), e)
            return result

        commits = await asyncio.to_thread(_collect_commits)

        # ── Publish RepositoryCloned ─────────────────────────────────────────
        size_kb = sum(f.stat().st_size for f in root.rglob("*") if f.is_file()) // 1024
        cloned_payload = RepositoryClonedPayload(
            repositoryId=repo_id,
            url=url,
            defaultBranch=branch,
            commitCount=len(commits),
            sizeKb=size_kb,
        )
        await publisher.publish(
            "repository.cloned",
            create_event("RepositoryCloned", repo_id, org_id, cloned_payload),
        )
        logger.info("RepositoryCloned published for %s (%d commits, %d KB)",
                    repo_id, len(commits), size_kb)

        # ── Dependency Detection ─────────────────────────────────────────────
        def _parse_deps():
            deps = []
            deps.extend(python_parser.parse_directory(root))
            deps.extend(node_parser.parse_directory(root))
            deps.extend(go_parser.parse_directory(root))
            deps.extend(rust_parser.parse_directory(root))
            return deps

        all_deps = await asyncio.to_thread(_parse_deps)
        logger.info("Found %d dependencies in %s", len(all_deps), repo_id)
        for dep in all_deps:
            dep_payload = DependencyDetectedPayload(
                repositoryId=repo_id,
                name=dep["name"],
                version=dep.get("version", ""),
                ecosystem=dep.get("ecosystem", "unknown"),
                sourceFile=dep.get("sourceFile", ""),
            )
            await publisher.publish(
                "dependency.detected",
                create_event("DependencyDetected", repo_id, org_id, dep_payload),
            )

        # ── Commit Analysis ──────────────────────────────────────────────────
        published_commits = 0
        for commit in commits:
            commit_payload = CommitAnalyzedPayload(
                repositoryId=repo_id,
                sha=commit["sha"],
                authorEmail=commit["author_email"],
                authorName=commit["author_name"],
                message=commit["message"],
                filesChanged=commit["files_changed"],
                committedAt=commit["committed_at"],
            )
            await publisher.publish(
                "commit.analyzed",
                create_event("CommitAnalyzed", commit["sha"], org_id, commit_payload),
            )
            published_commits += 1

        logger.info(
            "CommitAnalyzed: published %d/%d commits for %s",
            published_commits, len(commits), repo_id,
        )

        # ── Architecture Analysis (AST) ──────────────────────────────────────
        def _parse_ast():
            arch_symbols, arch_relations = [], []
            py_syms, py_rels = ast_python.parse_directory(root)
            arch_symbols.extend(py_syms); arch_relations.extend(py_rels)
            js_syms, js_rels = ast_javascript.parse_directory(root)
            arch_symbols.extend(js_syms); arch_relations.extend(js_rels)
            py_files = sum(1 for _ in root.rglob("*.py"))
            js_files = sum(1 for p in root.rglob("*")
                          if p.suffix in (".js", ".ts", ".jsx", ".tsx"))
            return arch_symbols, arch_relations, py_files + js_files

        try:
            arch_symbols, arch_relations, files_analyzed = await asyncio.to_thread(_parse_ast)
            logger.info("AST: %d symbols, %d relations across %d files",
                        len(arch_symbols), len(arch_relations), files_analyzed)
        except Exception as exc:
            logger.warning("AST analysis failed for %s: %s", repo_id, exc)
            arch_symbols, arch_relations, files_analyzed = [], [], 0

        if arch_symbols or arch_relations:
            seen_rels: set = set()
            unique_rels = []
            for r in arch_relations:
                key = (r["fromSymbol"], r["toSymbol"], r["relationType"])
                if key not in seen_rels:
                    seen_rels.add(key)
                    unique_rels.append(r)

            arch_payload = ArchitectureAnalyzedPayload(
                repositoryId=repo_id,
                language="multi",
                symbols=[CodeSymbol(**s) for s in arch_symbols[:2000]],
                relations=[CodeRelation(**r) for r in unique_rels[:5000]],
                filesAnalyzed=files_analyzed,
            )
            await publisher.publish(
                "architecture.analyzed",
                create_event("ArchitectureAnalyzed", repo_id, org_id, arch_payload),
            )
            logger.info(
                "ArchitectureAnalyzed published for %s: %d symbols, %d unique relations",
                repo_id, len(arch_symbols), len(unique_rels),
            )
        else:
            logger.info("No code symbols found for %s — skipping ArchitectureAnalyzed", repo_id)

        # ── ADR / Decision Scanning ───────────────────────────────────────────
        try:
            from parsers.adr_parser import parse_adr_files, parse_commit_decisions
            from shared.models import DecisionRecordedPayload

            # Parse ADR markdown files from clone dir
            adr_records = await asyncio.to_thread(
                parse_adr_files, clone_dir, repo_id
            )
            # Extract decision signals from commit messages
            # Normalize to the format expected by parse_commit_decisions
            commits_for_adr = [
                {
                    "sha":         c.get("sha", ""),
                    "message":     c.get("message", ""),
                    "committedAt": c.get("committed_at", ""),
                    "authorName":  c.get("author_name", ""),
                    "authorEmail": c.get("author_email", ""),
                }
                for c in commits
            ]
            commit_decisions = parse_commit_decisions(commits_for_adr, repo_id)

            all_decisions = adr_records + commit_decisions
            for dr in all_decisions:
                payload = DecisionRecordedPayload(
                    repositoryId=dr.repository_id,
                    title=dr.title,
                    status=dr.status,
                    context=dr.context,
                    decision=dr.decision,
                    consequences=dr.consequences,
                    sourceFile=dr.source_file,
                    sourceType=dr.source_type,
                    relatedEntities=dr.related_entities,
                    recordedAt=dr.recorded_at,
                )
                await publisher.publish(
                    "decision.recorded",
                    create_event("DecisionRecorded", repo_id, org_id, payload),
                )
            if all_decisions:
                logger.info(
                    "DecisionRecorded published for %s: %d ADRs, %d commit signals",
                    repo_id, len(adr_records), len(commit_decisions),
                )
        except Exception as exc:
            logger.warning("ADR parsing failed for %s: %s", repo_id, exc)

    finally:
        # Always remove the clone dir — even if an exception occurs above
        shutil.rmtree(clone_dir, ignore_errors=True)
        logger.info("Cleaned up clone dir: %s", clone_dir)


# ---------------------------------------------------------------------------
# Job runner — wraps _clone_and_analyze with lifecycle + retry
# ---------------------------------------------------------------------------

async def _run_job(job: JobState) -> None:
    """Execute a clone/analyze job with full lifecycle tracking and retry.

    Status transitions:
      QUEUED → (waiting for semaphore slot)
      QUEUED → RUNNING  (semaphore acquired)
      RUNNING → SUCCEEDED | FAILED
    """
    global _job_semaphore

    for attempt in range(1, GIT_MAX_RETRIES + 2):  # +2: initial + retries
        job.attempt = attempt
        job.error   = None
        logger.info("Job %s queued for slot (attempt %d/%d), repo %s",
                    job.job_id, attempt, GIT_MAX_RETRIES + 1, job.repo_id)

        # Acquire the concurrency slot — job stays QUEUED while waiting
        async with _job_semaphore:
            # Only mark RUNNING *after* we hold the slot
            job.status     = JobStatus.RUNNING
            job.started_at = _now_iso()
            logger.info("Job %s RUNNING (attempt %d/%d) for repo %s",
                        job.job_id, attempt, GIT_MAX_RETRIES + 1, job.repo_id)
            try:
                await asyncio.wait_for(
                    _clone_and_analyze(
                        job.repo_id, job.url, job.org_id,
                        job.branch, since_sha=job.since_sha,
                    ),
                    timeout=float(GIT_CLONE_TIMEOUT_SEC * 2),  # clone + analysis
                )
                job.status      = JobStatus.SUCCEEDED
                job.finished_at = _now_iso()
                logger.info("Job %s SUCCEEDED for repo %s", job.job_id, job.repo_id)
                return

            except asyncio.TimeoutError:
                job.status = JobStatus.QUEUED  # will retry
                job.error  = f"Timeout after {GIT_CLONE_TIMEOUT_SEC * 2}s"
                logger.error("Job %s TIMEOUT (attempt %d): %s", job.job_id, attempt, job.error)

            except Exception as exc:
                job.status = JobStatus.QUEUED  # will retry
                job.error  = str(exc)[:500]
                logger.error("Job %s FAILED (attempt %d): %s", job.job_id, attempt, job.error)

        if attempt <= GIT_MAX_RETRIES:
            backoff = 30 * (2 ** (attempt - 1))   # 30s, 60s
            logger.info("Job %s retrying in %ds", job.job_id, backoff)
            await asyncio.sleep(backoff)

    job.status      = JobStatus.FAILED
    job.finished_at = _now_iso()
    logger.error("Job %s exhausted all retries for repo %s", job.job_id, job.repo_id)


# ---------------------------------------------------------------------------
# Kafka event handler
# ---------------------------------------------------------------------------

async def handle_event(topic: str, value: dict) -> None:
    event_type = value.get("eventType", "")
    payload    = value.get("payload", {})
    org_id     = value.get("organizationId", "")

    if event_type == "RepositoryCreated":
        repo_id = payload.get("repositoryId", "")
        url     = payload.get("url", "")
        branch  = payload.get("defaultBranch", "main")
        if not url:
            logger.warning("RepositoryCreated event missing url — skipping clone")
            return
        logger.info("RepositoryCreated → queuing clone job for %s", repo_id)
        job = _make_job(repo_id, url, branch, org_id)
        _register_task(job.job_id, _run_job(job))

    elif event_type == "RepositorySyncRequested":
        repo_id   = payload.get("repositoryId", "")
        url       = payload.get("url", "")
        branch    = payload.get("defaultBranch", "main")
        since_sha = payload.get("sinceSha")   # optional incremental sync
        if not url:
            logger.warning("RepositorySyncRequested missing url — skipping")
            return
        # Try to get last SHA from graph-service for incremental sync
        if not since_sha:
            since_sha = await _fetch_last_sha(repo_id)
        logger.info("RepositorySyncRequested → queuing sync job for %s (since=%s)",
                    repo_id, since_sha or "full")
        job = _make_job(repo_id, url, branch, org_id, since_sha=since_sha)
        _register_task(job.job_id, _run_job(job))

    else:
        logger.debug("Ignoring event type: %s", event_type)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _job_semaphore
    _job_semaphore = asyncio.Semaphore(GIT_MAX_CONCURRENT)
    await publisher.start()
    await subscriber.start(handle_event)
    yield
    await subscriber.stop()
    await publisher.stop()


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Git Analyzer Service",
    description=(
        "Phase 2: Clones git repositories, detects dependencies, analyzes commit history, "
        "extracts architecture symbols and publishes domain events for downstream services. "
        "Features full job lifecycle management (queued/running/succeeded/failed) with "
        "retry and incremental sync support."
    ),
    version="2.0.0",
    lifespan=lifespan,
)


@app.get("/health", tags=["Operations"])
def health_check():
    running = sum(1 for j in _jobs.values() if j.status == JobStatus.RUNNING)
    queued  = sum(1 for j in _jobs.values() if j.status == JobStatus.QUEUED)
    return {
        "status":           "ok",
        "service":          "git-analyzer-service",
        "version":          "2.0.0",
        "maxCommits":       GIT_MAX_COMMITS,
        "cloneBaseDir":     GIT_CLONE_BASE_DIR,
        "cloneTimeoutSec":  GIT_CLONE_TIMEOUT_SEC,
        "maxConcurrent":    GIT_MAX_CONCURRENT,
        "maxRetries":       GIT_MAX_RETRIES,
        "jobs": {
            "total":     len(_jobs),
            "running":   running,
            "queued":    queued,
        },
    }


# ---------------------------------------------------------------------------
# Job status endpoints
# ---------------------------------------------------------------------------

@app.get("/jobs", tags=["Jobs"])
def list_jobs(status: Optional[str] = None, limit: int = 50):
    """List recent analysis jobs, optionally filtered by status."""
    jobs = list(_jobs.values())
    if status:
        jobs = [j for j in jobs if j.status == status]
    jobs.sort(key=lambda j: j.queued_at, reverse=True)
    return {
        "jobs":  [j.model_dump() for j in jobs[:limit]],
        "total": len(jobs),
    }


@app.get("/jobs/{job_id}", tags=["Jobs"])
def get_job(job_id: str):
    """Get status and details of a specific analysis job."""
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    return job.model_dump()


@app.delete("/jobs/{job_id}", tags=["Jobs"])
async def delete_job(job_id: str):
    """Cancel and remove a job from the store.

    - QUEUED jobs: task is cancelled (job will not start).
    - RUNNING jobs: task is cancelled (best-effort; asyncio.to_thread blocks
      are not interruptible, but the surrounding coroutine will be cancelled).
    - SUCCEEDED/FAILED jobs: removed from store (no task to cancel).
    """
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")

    # Snapshot status BEFORE any mutation so the response tells the truth
    previous_status = job.status

    # Cancel the asyncio task if it is still live
    task = _tasks.pop(job_id, None)
    if task and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        job.status      = JobStatus.FAILED
        job.error       = "Cancelled by user"
        job.finished_at = _now_iso()

    del _jobs[job_id]
    return {"cancelled": job_id, "previousStatus": previous_status}


# ---------------------------------------------------------------------------
# Analyze endpoint
# ---------------------------------------------------------------------------

class AnalyzeRequest(BaseModel):
    repositoryId:   str
    url:            str
    organizationId: str
    defaultBranch:  str = "main"
    sinceSha:       Optional[str] = None   # force incremental from this SHA


@app.post("/analyze", status_code=202, tags=["Analysis"])
async def trigger_analyze(req: AnalyzeRequest):
    """
    Queue a repository analysis job.

    Returns a jobId that can be polled via GET /jobs/{jobId}.
    In production, analysis is also triggered automatically via Kafka events.
    """
    # Deduplicate: reject if there's already a running/queued job for this repo
    for job in _jobs.values():
        if job.repo_id == req.repositoryId and job.status in (
            JobStatus.QUEUED, JobStatus.RUNNING
        ):
            raise HTTPException(
                status_code=409,
                detail=f"A job for repository '{req.repositoryId}' is already "
                       f"{job.status} (jobId={job.job_id}).",
            )

    since_sha = req.sinceSha
    if not since_sha:
        since_sha = await _fetch_last_sha(req.repositoryId)

    job = _make_job(
        req.repositoryId, req.url, req.defaultBranch,
        req.organizationId, since_sha=since_sha,
    )
    _register_task(job.job_id, _run_job(job))
    return {
        "jobId":        job.job_id,
        "status":       job.status,
        "repositoryId": job.repo_id,
        "sinceSha":     job.since_sha,
        "message":      "Job queued. Poll GET /jobs/{jobId} for status.",
    }

