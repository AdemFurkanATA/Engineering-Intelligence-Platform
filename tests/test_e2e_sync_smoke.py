"""
tests/test_e2e_sync_smoke.py

End-to-end smoke test: "repository sync → clone → events → graph update → last_sha"

This exercises the full cross-service event pipeline in-process, without
Kafka, Neo4j, Docker or real remote git repos.

Architecture:
  1. A real bare git repository is created on disk (gitpython).
  2. git-analyzer-service's _clone_and_analyze() is called directly.
  3. A stub publisher captures every publish() call.
  4. Captured events are replayed through graph-service's handle_event() dispatcher.
  5. Graph-service HTTP API is queried to verify graph state.

Covers:
  - RepositoryCloned event published with correct metadata
  - CommitAnalyzed events (one per commit)
  - DependencyDetected events (requirements.txt parsed)
  - graph-service Repository + Commit + Dependency node creation
  - Incremental since_sha filtering (fewer commits on second run)
  - POST /analyze job lifecycle
  - DELETE /jobs previousStatus correctness
  - graph-service /health and /graph/nodes endpoints
"""
import asyncio
import importlib.util
import os
import subprocess
import sys
import types
import unittest.mock as mock

import pytest
from fastapi.testclient import TestClient

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


# ---------------------------------------------------------------------------
# Shared stub infrastructure
# ---------------------------------------------------------------------------

def _patch_infra():
    """Inject infrastructure stubs (NOT git — we need real GitPython here)."""
    fake_kafka = types.ModuleType("shared.kafka")
    fake_kafka.EventPublisher  = mock.MagicMock
    fake_kafka.EventSubscriber = mock.MagicMock

    fake_config = types.ModuleType("shared.config")
    fake_config.NEO4J_URI      = "bolt://localhost:7687"
    fake_config.NEO4J_USER     = "neo4j"
    fake_config.NEO4J_PASSWORD = "test"

    stubs = {
        "shared.kafka":     fake_kafka,
        "shared.config":    fake_config,
        "shared.database":  mock.MagicMock(),
        "asyncpg":          mock.MagicMock(),
        "aiokafka":         mock.MagicMock(),
        "neo4j":            mock.MagicMock(),
        "neo4j.exceptions": mock.MagicMock(),
    }
    for name, mod in stubs.items():
        sys.modules[name] = mod
    return stubs


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Capturing stub publisher
# ---------------------------------------------------------------------------

class _CapturingPublisher:
    def __init__(self):
        self.events: list[dict] = []

    async def publish(self, topic: str, event) -> None:
        payload = event.model_dump() if hasattr(event, "model_dump") else dict(event)
        self.events.append({"topic": topic, **payload})

    def by_topic(self, topic: str) -> list[dict]:
        return [e for e in self.events if e.get("topic") == topic]

    async def start(self): pass
    async def stop(self):  pass


# ---------------------------------------------------------------------------
# Module-level fixtures (loaded once for entire file)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def bare_repo(tmp_path_factory):
    """Create a real bare git repository with 3 commits and a requirements.txt.

    Returns a namedtuple(path, url, branch) where url uses 'file://' prefix
    so that GitPython's shallow clone (--depth) works correctly for local repos.
    """
    import collections
    BareRepo = collections.namedtuple("BareRepo", ["path", "url", "branch"])

    work = tmp_path_factory.mktemp("e2e_work") / "repo"
    work.mkdir(parents=True)
    bare = tmp_path_factory.mktemp("e2e_bare") / "repo.git"

    def rn(*args):
        subprocess.check_call(list(args),
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)

    # Force 'main' as the initial branch (works on git ≥2.28)
    try:
        rn("git", "init", "-b", "main", str(work))
    except subprocess.CalledProcessError:
        # Older git: init then rename
        rn("git", "init", str(work))
        rn("git", "-C", str(work), "checkout", "-b", "main")

    G = ["git", "-C", str(work)]
    rn(*G, "config", "user.email", "e2e@test.com")
    rn(*G, "config", "user.name",  "E2E Bot")

    # Commit 1 — requirements.txt
    req = work / "requirements.txt"
    req.write_text("fastapi==0.110.0\nrequests>=2.31.0\n")
    rn(*G, "add", "requirements.txt")
    rn(*G, "commit", "-m", "feat: initial setup with FastAPI dependency")

    # Commit 2 — source file
    src = work / "app.py"
    src.write_text("from fastapi import FastAPI\napp = FastAPI()\n")
    rn(*G, "add", "app.py")
    rn(*G, "commit", "-m", "feat: add FastAPI app entrypoint")

    # Commit 3 — update deps
    req.write_text("fastapi==0.110.0\nrequests>=2.31.0\nhttpx==0.27.0\n")
    rn(*G, "add", "requirements.txt")
    rn(*G, "commit", "-m", "fix: add httpx for async HTTP client")

    rn("git", "clone", "--bare", str(work), str(bare))

    # Use file:// URL so git honours --depth in local clones
    file_url = bare.as_uri()   # e.g. file:///C:/Users/...
    return BareRepo(path=bare, url=file_url, branch="main")


@pytest.fixture(scope="module")
def ga(tmp_path_factory, bare_repo):
    """Load git-analyzer-service once with REAL GitPython (not the conftest fake).

    The conftest injects a MagicMock for 'git' at collection time so that unit
    tests can run without GitPython installed.  For this E2E file we remove that
    stub and install the real 'git' (GitPython) package into sys.modules so that
    actual git clone operations work.
    """
    _patch_infra()
    sys.path.insert(0, os.path.join(_ROOT, "src", "git-analyzer-service"))
    sys.path.insert(0, os.path.join(_ROOT, "src"))

    # Remove the fake git stub injected by conftest so we get real GitPython
    for key in list(sys.modules):
        if key == "git" or key.startswith("git."):
            sys.modules.pop(key, None)

    # Import real GitPython (package name on PyPI is 'gitpython'; imports as 'git')
    import git as _real_git          # noqa: F401 — ensures 'git' is in sys.modules
    import git.exc as _real_git_exc  # noqa: F401

    spec = importlib.util.spec_from_file_location(
        "ga_e2e", os.path.join(_ROOT, "src", "git-analyzer-service", "main.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    mod.GIT_CLONE_BASE_DIR    = str(tmp_path_factory.mktemp("e2e_clones"))
    mod.GIT_CLONE_TIMEOUT_SEC = 60
    mod.GIT_MAX_COMMITS       = 50
    mod.GIT_MAX_RETRIES       = 0
    mod._job_semaphore        = asyncio.Semaphore(3)
    return mod


@pytest.fixture(scope="module")
def gs():
    """Load graph-service once; force in-memory mode (no Neo4j).

    After loading, replaces the MagicMock publisher (from _patch_infra stubs)
    with a real async-capable no-op publisher so that handle_event can
    'await publisher.publish(...)' without raising TypeError.
    """
    _patch_infra()
    sys.path.insert(0, os.path.join(_ROOT, "src", "graph-service"))
    sys.path.insert(0, os.path.join(_ROOT, "src"))

    spec = importlib.util.spec_from_file_location(
        "gs_e2e", os.path.join(_ROOT, "src", "graph-service", "main.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._driver = None

    # Replace the MagicMock publisher with a real async no-op so that
    # handle_event's `await publisher.publish(...)` does not raise TypeError.
    class _AsyncNoOpPublisher:
        async def publish(self, *args, **kwargs): pass
        async def start(self): pass
        async def stop(self):  pass

    mod.publisher = _AsyncNoOpPublisher()
    return mod


# ---------------------------------------------------------------------------
# Per-test helpers (fresh publisher + cleared graph state each time)
# ---------------------------------------------------------------------------

@pytest.fixture()
def pub(ga):
    """Fresh capturing publisher injected into git-analyzer per test."""
    p = _CapturingPublisher()
    ga.publisher = p
    return p


@pytest.fixture()
def gs_client(gs):
    """Fresh graph state + HTTP client per test."""
    gs._nodes.clear()
    gs._relationships.clear()
    if hasattr(gs, "_decisions"):
        gs._decisions.clear()
    return TestClient(gs.app, raise_server_exceptions=True)


@pytest.fixture()
def ga_client(ga):
    """HTTP client for git-analyzer."""
    # Clear jobs between tests
    ga._jobs.clear()
    ga._tasks.clear()
    return TestClient(ga.app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# Helper: run analysis + optionally feed events to graph-service
# ---------------------------------------------------------------------------

def _analyze(ga, bare_repo, repo_id: str, since_sha: str = None):
    _run(ga._clone_and_analyze(
        repo_id, bare_repo.url, "org-1", bare_repo.branch, since_sha=since_sha
    ))


def _feed(gs, pub):
    """Replay captured events through graph-service handle_event.

    git-analyzer publishes events whose model_dump() produces snake_case fields:
      { "event_type": "RepositoryCloned", "organization_id": "…", "payload": {…} }

    graph-service handle_event expects camelCase:
      { "eventType": "RepositoryCloned", "organizationId": "…", "payload": {…} }

    This helper translates the schema before replaying.
    """
    relay_topics = {
        "repository.cloned", "repository.clone_failed",
        "commit.analyzed", "dependency.detected",
        "architecture.analyzed", "decision.recorded",
    }
    for ev in pub.events:
        if ev.get("topic") not in relay_topics:
            continue
        # Translate snake_case → camelCase for graph-service dispatcher
        translated = {
            "eventType":      ev.get("event_type") or ev.get("eventType", ""),
            "eventId":        ev.get("event_id")   or ev.get("eventId", ""),
            "organizationId": ev.get("organization_id") or ev.get("organizationId", ""),
            "payload":        ev.get("payload", {}),
        }
        _run(gs.handle_event(ev["topic"], translated))


# ===========================================================================
# Tests
# ===========================================================================

class TestE2ESyncPipeline:
    """Full pipeline: bare git repo → clone → events → graph nodes → API."""

    # -----------------------------------------------------------------------
    # 1. RepositoryCloned event
    # -----------------------------------------------------------------------

    def test_repository_cloned_event_published(self, ga, pub, bare_repo):
        _analyze(ga, bare_repo, "e2e-r1")
        evs = pub.by_topic("repository.cloned")
        assert len(evs) == 1
        p = evs[0].get("payload", {})
        assert p.get("repositoryId") == "e2e-r1"
        assert p.get("commitCount", 0) >= 1

    # -----------------------------------------------------------------------
    # 2. CommitAnalyzed events (one per commit)
    # -----------------------------------------------------------------------

    def test_commit_analyzed_events_published(self, ga, pub, bare_repo):
        _analyze(ga, bare_repo, "e2e-r2")
        commits = pub.by_topic("commit.analyzed")
        assert len(commits) >= 3, f"Expected ≥3 commits, got {len(commits)}"
        for ev in commits:
            p = ev.get("payload", {})
            assert p.get("sha"),        "Commit event missing sha"
            assert p.get("committedAt"), "Commit event missing committedAt"

    # -----------------------------------------------------------------------
    # 3. DependencyDetected events (requirements.txt)
    # -----------------------------------------------------------------------

    def test_dependency_detected_events_published(self, ga, pub, bare_repo):
        _analyze(ga, bare_repo, "e2e-r3")
        deps = pub.by_topic("dependency.detected")
        dep_names = {e.get("payload", {}).get("name", "").lower() for e in deps}
        assert "fastapi"  in dep_names, f"fastapi not in deps: {dep_names}"
        assert "requests" in dep_names, f"requests not in deps: {dep_names}"

    # -----------------------------------------------------------------------
    # 4. Repository node created in graph
    # -----------------------------------------------------------------------

    def test_graph_has_repository_node_after_clone_event(self, ga, pub, gs, gs_client, bare_repo):
        _analyze(ga, bare_repo, "e2e-r4")
        _feed(gs, pub)

        r = gs_client.get("/graph/nodes?label=Repository")
        assert r.status_code == 200
        # Repository nodes: node_id IS the repositoryId; check nodeId field
        node_ids = [
            n["nodeId"]
            for n in r.json()["data"]
            if n.get("nodeId") == "e2e-r4" or n.get("properties", {}).get("repositoryId") == "e2e-r4"
        ]
        assert len(node_ids) >= 1, (
            f"Repository node for 'e2e-r4' not created. Nodes: "
            f"{[n.get('nodeId') for n in r.json()['data']]}"
        )

    # -----------------------------------------------------------------------
    # 5. Commit nodes created in graph
    # -----------------------------------------------------------------------

    def test_graph_has_commit_nodes(self, ga, pub, gs, gs_client, bare_repo):
        _analyze(ga, bare_repo, "e2e-r5")
        _feed(gs, pub)

        r = gs_client.get("/graph/nodes?label=Commit")
        assert r.status_code == 200
        nodes = [
            n for n in r.json()["data"]
            if n.get("properties", {}).get("repositoryId") == "e2e-r5"
        ]
        assert len(nodes) >= 3, f"Expected ≥3 Commit nodes, got {len(nodes)}"

    # -----------------------------------------------------------------------
    # 6. Dependency nodes created in graph
    # -----------------------------------------------------------------------

    def test_graph_has_dependency_nodes(self, ga, pub, gs, gs_client, bare_repo):
        _analyze(ga, bare_repo, "e2e-r6")
        _feed(gs, pub)

        r = gs_client.get("/graph/nodes?label=Dependency")
        assert r.status_code == 200
        # Dependency nodes don't carry repositoryId as a property (it's in the
        # DEPENDS_ON relationship). Check by name across all Dependency nodes.
        dep_names = {
            n["properties"].get("name", "").lower()
            for n in r.json()["data"]
        }
        assert "fastapi" in dep_names, f"fastapi Dependency node missing: {dep_names}"
        assert "requests" in dep_names, f"requests Dependency node missing: {dep_names}"

    # -----------------------------------------------------------------------
    # 7. Incremental sync: since_sha skips already-processed commits
    # -----------------------------------------------------------------------

    def test_incremental_since_sha_skips_old_commits(self, ga, pub, bare_repo):
        _analyze(ga, bare_repo, "e2e-r7")
        all_commits = pub.by_topic("commit.analyzed")
        all_shas    = [e["payload"]["sha"] for e in all_commits]
        assert len(all_shas) >= 2, "Need at least 2 commits for incremental test"

        # Clear publisher; re-run incremental
        pub.events.clear()
        oldest_sha = all_shas[-1]   # iter_commits() is newest-first
        _analyze(ga, bare_repo, "e2e-r7", since_sha=oldest_sha)

        incremental = pub.by_topic("commit.analyzed")
        assert len(incremental) < len(all_shas), (
            f"Incremental produced {len(incremental)} commits but full run had {len(all_shas)}"
        )

    # -----------------------------------------------------------------------
    # 8. POST /analyze → job queued
    # -----------------------------------------------------------------------

    def test_analyze_endpoint_queues_job(self, bare_repo, ga_client):
        r = ga_client.post("/analyze", json={
            "repositoryId":   "e2e-api-repo",
            "url":            bare_repo.url,
            "organizationId": "org-e2e",
            "defaultBranch":  bare_repo.branch,
        })
        assert r.status_code == 202
        body = r.json()
        assert "jobId" in body

        r2 = ga_client.get(f"/jobs/{body['jobId']}")
        assert r2.status_code == 200

    # -----------------------------------------------------------------------
    # 9. DELETE /jobs returns correct previousStatus
    # -----------------------------------------------------------------------

    def test_delete_job_returns_correct_previous_status(self, ga, ga_client):
        job = ga._make_job("e2e-cancel", "https://x.com/r", "main", "org")
        job.status = ga.JobStatus.QUEUED

        r = ga_client.delete(f"/jobs/{job.job_id}")
        assert r.status_code == 200
        body = r.json()
        assert body["previousStatus"] == "queued"
        assert body["cancelled"]      == job.job_id

    # -----------------------------------------------------------------------
    # 10. graph-service /health
    # -----------------------------------------------------------------------

    def test_graph_service_health(self, gs_client):
        r = gs_client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"]  == "ok"
        assert r.json()["backend"] == "in-memory"

    # -----------------------------------------------------------------------
    # 11. graph-service /graph/nodes returns list
    # -----------------------------------------------------------------------

    def test_graph_nodes_endpoint_returns_list(self, gs_client):
        r = gs_client.get("/graph/nodes")
        assert r.status_code == 200
        assert isinstance(r.json()["data"], list)
