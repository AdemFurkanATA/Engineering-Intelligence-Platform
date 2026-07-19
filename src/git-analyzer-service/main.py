"""
git-analyzer-service — FastAPI application.

Phase 2 responsibilities:
- Listen to repository.created and repository.sync_requested events
- Clone the repository to a temporary directory using GitPython
- Detect and parse dependencies (Python/Node/Go/Rust)
- Read commit history (last GIT_MAX_COMMITS commits)
- Publish domain events:
    RepositoryCloned      → repository.cloned
    DependencyDetected    → dependency.detected  (one per dependency)
    CommitAnalyzed        → commit.analyzed      (one per commit)

Storage: stateless — all state is in Kafka events and downstream services.
Clones are written to a temp directory and deleted after analysis.

Configuration (env vars):
    GIT_MAX_COMMITS        int   default 100
    GIT_CLONE_TIMEOUT_SEC  int   default 120
    GIT_CLONE_DIR          str   default /tmp/eip-clones
    KAFKA_BOOTSTRAP_SERVERS str  default localhost:9092
"""
import logging
import os
import shutil
import sys
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from shared.kafka import EventPublisher, EventSubscriber
from shared.models import (
    CommitAnalyzedPayload,
    DependencyDetectedPayload,
    RepositoryClonedPayload,
    create_event,
)
from parsers import python_parser, node_parser, go_parser, rust_parser

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


async def _clone_and_analyze(repo_id: str, url: str, org_id: str, branch: str = "main") -> None:
    """Clone the repository, run all parsers, publish events. Cleans up on exit."""
    clone_dir = os.path.join(GIT_CLONE_BASE_DIR, f"eip-{repo_id}")
    try:
        import git as gitpython
    except ImportError:
        logger.error(
            "GitPython not installed — cannot clone repo %s. "
            "Install gitpython in the service container.", repo_id
        )
        return

    # ── Clone ──────────────────────────────────────────────────────────────
    if os.path.exists(clone_dir):
        shutil.rmtree(clone_dir, ignore_errors=True)

    logger.info("Cloning %s → %s", url, clone_dir)
    try:
        repo = gitpython.Repo.clone_from(
            url, clone_dir,
            depth=GIT_MAX_COMMITS,           # shallow clone for speed
            no_single_branch=True,
            kill_after_timeout=GIT_CLONE_TIMEOUT_SEC,
        )
    except Exception as exc:
        logger.error("Clone failed for %s: %s", url, exc)
        return

    root = Path(clone_dir)

    # Count commits (shallow clone may differ from actual total)
    try:
        commits = list(repo.iter_commits(max_count=GIT_MAX_COMMITS))
    except Exception:
        commits = []

    # ── Publish RepositoryCloned ───────────────────────────────────────────
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
    logger.info("RepositoryCloned published for %s (%d commits, %d KB)", repo_id, len(commits), size_kb)

    # ── Dependency Detection ────────────────────────────────────────────────
    all_deps = []
    all_deps.extend(python_parser.parse_directory(root))
    all_deps.extend(node_parser.parse_directory(root))
    all_deps.extend(go_parser.parse_directory(root))
    all_deps.extend(rust_parser.parse_directory(root))

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

    # ── Commit Analysis ─────────────────────────────────────────────────────
    published_commits = 0
    for commit in commits:
        try:
            committed_dt = datetime.fromtimestamp(
                commit.committed_date, tz=timezone.utc
            )
            files_changed = list(commit.stats.files.keys())[:50]  # cap per commit

            commit_payload = CommitAnalyzedPayload(
                repositoryId=repo_id,
                sha=commit.hexsha,
                authorEmail=commit.author.email or "",
                authorName=commit.author.name or "",
                message=(commit.message or "").strip()[:500],
                filesChanged=files_changed,
                committedAt=committed_dt,
            )
            await publisher.publish(
                "commit.analyzed",
                create_event("CommitAnalyzed", commit.hexsha, org_id, commit_payload),
            )
            published_commits += 1
        except Exception as exc:
            logger.warning("Skipping commit %s: %s", getattr(commit, "hexsha", "?"), exc)

    logger.info(
        "CommitAnalyzed: published %d/%d commits for %s",
        published_commits, len(commits), repo_id,
    )

    # ── Cleanup ─────────────────────────────────────────────────────────────
    shutil.rmtree(clone_dir, ignore_errors=True)
    logger.info("Cleaned up clone dir: %s", clone_dir)


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
        logger.info("RepositoryCreated → starting clone for %s", repo_id)
        await _clone_and_analyze(repo_id, url, org_id, branch)

    elif event_type == "RepositorySyncRequested":
        repo_id = payload.get("repositoryId", "")
        url     = payload.get("url", "")
        branch  = payload.get("defaultBranch", "main")
        if not url:
            logger.warning("RepositorySyncRequested missing url — skipping")
            return
        logger.info("RepositorySyncRequested → re-cloning %s", repo_id)
        await _clone_and_analyze(repo_id, url, org_id, branch)

    else:
        logger.debug("Ignoring event type: %s", event_type)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
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
        "Phase 2: Clones git repositories, detects dependencies, analyzes commit history "
        "and publishes domain events for downstream graph/search/embedding services."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health", tags=["Operations"])
def health_check():
    return {
        "status":         "ok",
        "service":        "git-analyzer-service",
        "maxCommits":     GIT_MAX_COMMITS,
        "cloneBaseDir":   GIT_CLONE_BASE_DIR,
        "cloneTimeoutSec": GIT_CLONE_TIMEOUT_SEC,
    }


class AnalyzeRequest(BaseModel):
    repositoryId:  str
    url:           str
    organizationId: str
    defaultBranch: str = "main"


@app.post("/analyze", tags=["Analysis"])
async def trigger_analyze(req: AnalyzeRequest):
    """
    Manually trigger a repository analysis (for testing / manual sync).
    In production, analysis is triggered automatically via Kafka events.
    """
    import asyncio
    asyncio.create_task(
        _clone_and_analyze(req.repositoryId, req.url, req.organizationId, req.defaultBranch)
    )
    return {
        "status":       "started",
        "repositoryId": req.repositoryId,
        "url":          req.url,
        "message":      "Analysis started in background. Monitor logs for progress.",
    }
