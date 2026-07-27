"""
tests/test_analysis.py

Unit tests for Phase 2.2:
  - Dependency Analyzer (circular dep, critical nodes, bottleneck, ecosystem)
  - Timeline Engine (commit + dependency events, filtering, ordering)
  - RepositoryCloneFailed event handling in graph-service

Run with:
  PYTHONPATH=src python -m pytest tests/test_analysis.py -v
"""
import sys, os, types, asyncio, unittest.mock as mock
from datetime import datetime, timezone, timedelta

_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(_ROOT, "src"))

import pytest

# ---------------------------------------------------------------------------
# Load graph-service module with mocked dependencies
# ---------------------------------------------------------------------------

def _load_graph_module():
    import importlib.util
    mods = {
        "shared.kafka":   types.SimpleNamespace(EventPublisher=mock.MagicMock, EventSubscriber=mock.MagicMock),
        "shared.config":  types.SimpleNamespace(NEO4J_URI="bolt://x", NEO4J_USER="u", NEO4J_PASSWORD="p"),
        "shared.database":mock.MagicMock(),
        "asyncpg":        mock.MagicMock(),
        "neo4j":          mock.MagicMock(),
    }
    with mock.patch.dict("sys.modules", mods):
        spec = importlib.util.spec_from_file_location(
            "graph_main_analysis",
            os.path.join(_ROOT, "src/graph-service/main.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod

def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)

MOD = _load_graph_module()


def _reset(mod):
    mod._nodes = {}
    mod._relationships = []
    mod._driver = None


# ---------------------------------------------------------------------------
# Helpers to build in-memory graph state
# ---------------------------------------------------------------------------

def _add_repo(mod, repo_id, name="test-repo"):
    mod._nodes[repo_id] = {
        "nodeId": repo_id, "label": "Repository",
        "properties": {"name": name, "url": f"https://github.com/acme/{name}"},
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "updatedAt": datetime.now(timezone.utc).isoformat(),
    }


def _add_dep(mod, dep_id, name, ecosystem, version="1.0", repo_id=None):
    mod._nodes[dep_id] = {
        "nodeId": dep_id, "label": "Dependency",
        "properties": {"name": name, "ecosystem": ecosystem, "version": version},
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "updatedAt": datetime.now(timezone.utc).isoformat(),
    }
    if repo_id:
        mod._relationships.append({
            "relationshipId": f"rel-{dep_id}",
            "from": repo_id, "to": dep_id, "type": "DEPENDS_ON",
            "properties": {}, "createdAt": datetime.now(timezone.utc).isoformat(),
        })


def _add_commit(mod, sha, repo_id, message="feat: add feature", author="dev@x.com",
                author_name="Dev", ts=None):
    committed_at = ts or datetime.now(timezone.utc).isoformat()
    commit_id = f"commit:{sha}"
    mod._nodes[commit_id] = {
        "nodeId": commit_id, "label": "Commit",
        "properties": {
            "sha": sha, "message": message,
            "authorEmail": author, "authorName": author_name,
            "committedAt": committed_at, "filesChanged": 2,
            "repositoryId": repo_id,
        },
        "createdAt": committed_at,
        "updatedAt": committed_at,
    }


# =============================================================================
# Dependency Analyzer — in-memory
# =============================================================================

class TestMemDependencyAnalyzer:

    def setup_method(self):
        _reset(MOD)

    # ── Empty repo ──────────────────────────────────────────────────────────

    def test_empty_repo_returns_zero_deps(self):
        _add_repo(MOD, "r1")
        result = MOD._mem_dep_analysis("r1")
        assert result["totalDependencies"] == 0
        assert result["circularDependencies"] == []
        assert result["criticalNodes"] == []
        assert result["bottleneckScore"] == 0.0

    # ── Basic dependency detection ──────────────────────────────────────────

    def test_single_dep_counted(self):
        _add_repo(MOD, "r1")
        _add_dep(MOD, "dep:pip:requests:2.31", "requests", "pip", "2.31", "r1")
        result = MOD._mem_dep_analysis("r1")
        assert result["totalDependencies"] == 1

    def test_multiple_deps_counted(self):
        _add_repo(MOD, "r1")
        for name in ("fastapi", "pydantic", "httpx"):
            _add_dep(MOD, f"dep:pip:{name}:1.0", name, "pip", "1.0", "r1")
        result = MOD._mem_dep_analysis("r1")
        assert result["totalDependencies"] == 3

    # ── Ecosystem breakdown ─────────────────────────────────────────────────

    def test_ecosystem_breakdown_single(self):
        _add_repo(MOD, "r1")
        _add_dep(MOD, "dep:pip:requests:2.31", "requests", "pip", "2.31", "r1")
        result = MOD._mem_dep_analysis("r1")
        assert result["ecosystemBreakdown"].get("pip", 0) == 1

    def test_ecosystem_breakdown_mixed(self):
        _add_repo(MOD, "r1")
        _add_dep(MOD, "dep:pip:flask:3.0", "flask", "pip", "3.0", "r1")
        _add_dep(MOD, "dep:npm:express:4.18", "express", "npm", "4.18", "r1")
        result = MOD._mem_dep_analysis("r1")
        breakdown = result["ecosystemBreakdown"]
        assert breakdown.get("pip", 0) == 1
        assert breakdown.get("npm", 0) == 1

    # ── Critical nodes ──────────────────────────────────────────────────────

    def test_critical_node_detected_when_used_by_multiple_repos(self):
        # Two repos share the same dependency → inDegree >= 2
        _add_repo(MOD, "r1", "svc-a")
        _add_repo(MOD, "r2", "svc-b")
        dep_id = "dep:pip:requests:2.31"
        _add_dep(MOD, dep_id, "requests", "pip", "2.31", "r1")
        _add_dep(MOD, dep_id, "requests", "pip", "2.31", "r2")  # second DEPENDS_ON edge
        result = MOD._mem_dep_analysis("r1")
        critical_names = [c["name"] for c in result["criticalNodes"]]
        assert "requests" in critical_names

    def test_non_critical_node_not_in_critical_list(self):
        # Single repo uses the dep → inDegree == 1, not critical
        _add_repo(MOD, "r1")
        _add_dep(MOD, "dep:pip:rare:0.1", "rare-package", "pip", "0.1", "r1")
        result = MOD._mem_dep_analysis("r1")
        # With only one repo using it, inDegree = 1 < threshold 2
        assert result["criticalNodes"] == []

    def test_critical_node_has_score_between_0_and_1(self):
        _add_repo(MOD, "r1", "svc-a")
        _add_repo(MOD, "r2", "svc-b")
        _add_repo(MOD, "r3", "svc-c")
        dep_id = "dep:pip:shared:1.0"
        for repo_id in ("r1", "r2", "r3"):
            _add_dep(MOD, dep_id, "shared-lib", "pip", "1.0", repo_id)
        result = MOD._mem_dep_analysis("r1")
        for c in result["criticalNodes"]:
            assert 0 <= c["score"] <= 1

    # ── Bottleneck score ────────────────────────────────────────────────────

    def test_bottleneck_score_zero_when_no_deps(self):
        _add_repo(MOD, "r1")
        result = MOD._mem_dep_analysis("r1")
        assert result["bottleneckScore"] == 0.0

    def test_bottleneck_score_normalized_0_to_1(self):
        _add_repo(MOD, "r1")
        _add_repo(MOD, "r2")
        dep_id = "dep:pip:shared:1.0"
        for rid in ("r1", "r2"):
            _add_dep(MOD, dep_id, "shared", "pip", "1.0", rid)
        result = MOD._mem_dep_analysis("r1")
        assert 0.0 <= result["bottleneckScore"] <= 1.0

    # ── Circular dependency ─────────────────────────────────────────────────

    def test_no_circular_deps_in_clean_graph(self):
        _add_repo(MOD, "r1")
        _add_dep(MOD, "dep:pip:requests:2.31", "requests", "pip", "2.31", "r1")
        result = MOD._mem_dep_analysis("r1")
        assert result["circularDependencies"] == []

    def test_circular_dep_detected(self):
        # Simulate: r1 DEPENDS_ON dep, dep DEPENDS_ON r1 (circular)
        _add_repo(MOD, "r1")
        dep_id = "dep:pip:circular:1.0"
        _add_dep(MOD, dep_id, "circular-pkg", "pip", "1.0", "r1")
        # Add back-edge: dep → repo (simulated circular)
        MOD._relationships.append({
            "relationshipId": "back-edge",
            "from": dep_id, "to": "r1", "type": "DEPENDS_ON",
            "properties": {}, "createdAt": "",
        })
        result = MOD._mem_dep_analysis("r1")
        assert "circular-pkg" in result["circularDependencies"]


# =============================================================================
# Critical dependencies (platform-wide) — in-memory
# =============================================================================

class TestCriticalDependenciesEndpoint:
    """Test the /graph/analysis/dependencies/critical in-memory logic."""

    def setup_method(self):
        _reset(MOD)

    def test_no_deps_returns_empty_list(self):
        from fastapi.testclient import TestClient
        MOD._driver = None
        client = TestClient(MOD.app)
        r = client.get("/graph/analysis/dependencies/critical")
        assert r.status_code == 200
        assert r.json()["criticalDependencies"] == []

    def test_threshold_filters_correctly(self):
        from fastapi.testclient import TestClient
        MOD._driver = None
        _add_repo(MOD, "r1"); _add_repo(MOD, "r2"); _add_repo(MOD, "r3")
        dep_id = "dep:pip:popular:1.0"
        for rid in ("r1", "r2", "r3"):
            _add_dep(MOD, dep_id, "popular-lib", "pip", "1.0", rid)
        client = TestClient(MOD.app)
        # threshold=2: popular-lib used by 3 repos → included
        r = client.get("/graph/analysis/dependencies/critical?threshold=2")
        names = [d["name"] for d in r.json()["criticalDependencies"]]
        assert "popular-lib" in names
        # threshold=4: popular-lib used by only 3 → excluded
        r4 = client.get("/graph/analysis/dependencies/critical?threshold=4")
        assert r4.json()["criticalDependencies"] == []

    def test_criticality_score_present(self):
        from fastapi.testclient import TestClient
        MOD._driver = None
        _add_repo(MOD, "r1"); _add_repo(MOD, "r2")
        dep_id = "dep:pip:shared:2.0"
        for rid in ("r1", "r2"):
            _add_dep(MOD, dep_id, "shared", "pip", "2.0", rid)
        client = TestClient(MOD.app)
        r = client.get("/graph/analysis/dependencies/critical?threshold=2")
        deps = r.json()["criticalDependencies"]
        assert all("criticalityScore" in d for d in deps)


# =============================================================================
# Ecosystem breakdown endpoint — in-memory
# =============================================================================

class TestEcosystemBreakdown:

    def setup_method(self):
        _reset(MOD)

    def test_empty_graph_returns_zero(self):
        from fastapi.testclient import TestClient
        MOD._driver = None
        client = TestClient(MOD.app)
        r = client.get("/graph/analysis/dependencies/ecosystem")
        assert r.status_code == 200
        assert r.json()["totalDependencies"] == 0

    def test_mixed_ecosystems_counted_correctly(self):
        from fastapi.testclient import TestClient
        MOD._driver = None
        _add_repo(MOD, "r1")
        _add_dep(MOD, "dep:pip:flask:3", "flask", "pip", "3.0", "r1")
        _add_dep(MOD, "dep:pip:requests:2", "requests", "pip", "2.31", "r1")
        _add_dep(MOD, "dep:npm:express:4", "express", "npm", "4.18", "r1")
        client = TestClient(MOD.app)
        r = client.get("/graph/analysis/dependencies/ecosystem")
        data = r.json()
        assert data["ecosystems"].get("pip", 0) == 2
        assert data["ecosystems"].get("npm", 0) == 1
        assert data["totalDependencies"] == 3

    def test_unique_packages_counted(self):
        from fastapi.testclient import TestClient
        MOD._driver = None
        _add_repo(MOD, "r1"); _add_repo(MOD, "r2")
        # Same package used by two repos — should count as 1 unique
        dep_id = "dep:pip:requests:2.31"
        _add_dep(MOD, dep_id, "requests", "pip", "2.31", "r1")
        _add_dep(MOD, dep_id, "requests", "pip", "2.31", "r2")
        client = TestClient(MOD.app)
        r = client.get("/graph/analysis/dependencies/ecosystem")
        assert r.json()["uniquePackages"] == 1


# =============================================================================
# Timeline Engine — in-memory
# =============================================================================

class TestTimelineEngine:

    def setup_method(self):
        _reset(MOD)

    def _ts(self, days_ago=0):
        """Return ISO timestamp N days in the past."""
        return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()

    def test_empty_repo_returns_empty_events(self):
        from fastapi.testclient import TestClient
        MOD._driver = None
        _add_repo(MOD, "r1")
        client = TestClient(MOD.app)
        r = client.get("/graph/timeline/r1")
        assert r.status_code == 200
        assert r.json()["events"] == []
        assert r.json()["total"] == 0

    def test_nonexistent_repo_returns_404(self):
        from fastapi.testclient import TestClient
        MOD._driver = None
        client = TestClient(MOD.app)
        r = client.get("/graph/timeline/ghost-repo")
        assert r.status_code == 404

    def test_commit_events_appear_in_timeline(self):
        from fastapi.testclient import TestClient
        MOD._driver = None
        _add_repo(MOD, "r1")
        _add_commit(MOD, "abc123", "r1", "feat: add auth", ts=self._ts(1))
        client = TestClient(MOD.app)
        r = client.get("/graph/timeline/r1")
        events = r.json()["events"]
        assert any(e["type"] == "commit" and e["sha"] == "abc123" for e in events)

    def test_dependency_events_appear_in_timeline(self):
        from fastapi.testclient import TestClient
        MOD._driver = None
        _add_repo(MOD, "r1")
        _add_dep(MOD, "dep:pip:fastapi:0.110", "fastapi", "pip", "0.110", "r1")
        client = TestClient(MOD.app)
        r = client.get("/graph/timeline/r1")
        events = r.json()["events"]
        assert any(e["type"] == "dependency_added" and e["dependencyName"] == "fastapi" for e in events)

    def test_events_sorted_descending_by_timestamp(self):
        from fastapi.testclient import TestClient
        MOD._driver = None
        _add_repo(MOD, "r1")
        _add_commit(MOD, "old001", "r1", "old commit", ts=self._ts(10))
        _add_commit(MOD, "new001", "r1", "new commit", ts=self._ts(1))
        client = TestClient(MOD.app)
        r = client.get("/graph/timeline/r1")
        events = r.json()["events"]
        commit_events = [e for e in events if e["type"] == "commit"]
        assert len(commit_events) >= 2
        timestamps = [e["timestamp"] for e in commit_events]
        assert timestamps == sorted(timestamps, reverse=True)

    def test_filter_commit_only(self):
        from fastapi.testclient import TestClient
        MOD._driver = None
        _add_repo(MOD, "r1")
        _add_commit(MOD, "abc123", "r1", "feat: x")
        _add_dep(MOD, "dep:pip:a:1", "a-lib", "pip", "1.0", "r1")
        client = TestClient(MOD.app)
        r = client.get("/graph/timeline/r1?event_type=commit")
        events = r.json()["events"]
        assert all(e["type"] == "commit" for e in events)

    def test_filter_dependency_only(self):
        from fastapi.testclient import TestClient
        MOD._driver = None
        _add_repo(MOD, "r1")
        _add_commit(MOD, "abc123", "r1", "feat: x")
        _add_dep(MOD, "dep:pip:a:1", "a-lib", "pip", "1.0", "r1")
        client = TestClient(MOD.app)
        r = client.get("/graph/timeline/r1?event_type=dependency")
        events = r.json()["events"]
        assert all(e["type"] == "dependency_added" for e in events)

    def test_invalid_event_type_returns_422(self):
        from fastapi.testclient import TestClient
        MOD._driver = None
        _add_repo(MOD, "r1")
        client = TestClient(MOD.app)
        r = client.get("/graph/timeline/r1?event_type=invalid")
        assert r.status_code == 422

    def test_limit_respected(self):
        from fastapi.testclient import TestClient
        MOD._driver = None
        _add_repo(MOD, "r1")
        for i in range(20):
            _add_commit(MOD, f"sha{i:04d}", "r1", f"commit {i}",
                        ts=self._ts(20 - i))
        client = TestClient(MOD.app)
        r = client.get("/graph/timeline/r1?limit=5")
        assert len(r.json()["events"]) <= 5

    def test_since_filter_excludes_old_events(self):
        from fastapi.testclient import TestClient
        MOD._driver = None
        _add_repo(MOD, "r1")
        _add_commit(MOD, "old", "r1", "old", ts=self._ts(30))
        _add_commit(MOD, "new", "r1", "new", ts=self._ts(1))
        since = self._ts(7)  # last 7 days
        client = TestClient(MOD.app)
        r = client.get(f"/graph/timeline/r1?event_type=commit&since={since}")
        shas = [e["sha"] for e in r.json()["events"] if e["type"] == "commit"]
        assert "new" in shas
        assert "old" not in shas

    def test_response_contains_metadata_fields(self):
        from fastapi.testclient import TestClient
        MOD._driver = None
        _add_repo(MOD, "r1")
        client = TestClient(MOD.app)
        r = client.get("/graph/timeline/r1")
        data = r.json()
        assert "repositoryId" in data
        assert "generatedAt" in data
        assert "eventType" in data


# =============================================================================
# RepositoryCloneFailed — shared model + graph handler
# =============================================================================

class TestRepositoryCloneFailed:

    def test_payload_model_fields(self):
        from shared.models import RepositoryCloneFailedPayload
        p = RepositoryCloneFailedPayload(
            repositoryId="r1",
            url="https://github.com/acme/app",
            error="Connection timed out",
        )
        assert p.repository_id == "r1"
        assert p.error == "Connection timed out"
        assert p.failed_at is not None

    def test_payload_model_error_defaults_empty(self):
        from shared.models import RepositoryCloneFailedPayload
        p = RepositoryCloneFailedPayload(repositoryId="r1", url="https://x.com")
        assert p.error == ""

    def test_event_serialization(self):
        from shared.models import RepositoryCloneFailedPayload, create_event
        p = RepositoryCloneFailedPayload(
            repositoryId="r1", url="https://x.com", error="timeout"
        )
        env = create_event("RepositoryCloneFailed", "r1", "org_1", p)
        data = env.model_dump(by_alias=True)
        assert data["eventType"] == "RepositoryCloneFailed"
        assert data["payload"]["error"] == "timeout"

    def test_graph_handler_marks_clone_status_failed(self):
        _reset(MOD)
        _add_repo(MOD, "r1")
        run(MOD.handle_event("repository.clone_failed", {
            "eventType": "RepositoryCloneFailed",
            "eventId": "e1",
            "organizationId": "org_1",
            "payload": {
                "repositoryId": "r1",
                "url": "https://x.com",
                "error": "Connection refused",
                "failedAt": datetime.now(timezone.utc).isoformat(),
            },
        }))
        node = MOD._nodes.get("r1", {})
        assert node["properties"].get("cloneStatus") == "failed"

    def test_graph_handler_stores_error_message(self):
        _reset(MOD)
        _add_repo(MOD, "r1")
        run(MOD.handle_event("repository.clone_failed", {
            "eventType": "RepositoryCloneFailed",
            "eventId": "e1",
            "organizationId": "org_1",
            "payload": {
                "repositoryId": "r1",
                "url": "https://x.com",
                "error": "Authentication failed",
                "failedAt": "",
            },
        }))
        node = MOD._nodes.get("r1", {})
        assert "Authentication failed" in node["properties"].get("cloneError", "")

    def test_graph_handler_graceful_for_unknown_repo(self):
        """RepositoryCloneFailed for an unknown repo should not crash."""
        _reset(MOD)
        # No repo node — handler should still run (upsert creates the node)
        run(MOD.handle_event("repository.clone_failed", {
            "eventType": "RepositoryCloneFailed",
            "eventId": "e1",
            "organizationId": "org_1",
            "payload": {
                "repositoryId": "unknown-repo",
                "url": "https://x.com",
                "error": "not found",
                "failedAt": "",
            },
        }))
        # upsert should have created a minimal node
        assert "unknown-repo" in MOD._nodes
