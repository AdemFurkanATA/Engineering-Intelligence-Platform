"""
tests/test_integration.py

FastAPI TestClient integration tests for the Engineering Intelligence Platform.

These tests exercise the HTTP route layer directly, verifying request/response
schemas, status codes, and the correctness of service-level business logic
without requiring Kafka, PostgreSQL, Neo4j, or Qdrant to be running.

Run with:
  PYTHONPATH=src python -m pytest tests/test_integration.py -v
"""
import sys
import os
import types
import unittest.mock as mock

_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(_ROOT, "src"))
sys.path.insert(0, os.path.join(_ROOT, "src", "search-service"))

import pytest
from fastapi.testclient import TestClient


# =============================================================================
# helpers
# =============================================================================

def _stub_modules(extra=None):
    fake_kafka = types.ModuleType("shared.kafka")
    fake_kafka.EventPublisher  = mock.MagicMock
    fake_kafka.EventSubscriber = mock.MagicMock
    fake_config = types.ModuleType("shared.config")
    fake_config.QDRANT_URL     = "http://localhost:6333"
    fake_config.NEO4J_URI      = "bolt://localhost:7687"
    fake_config.NEO4J_USER     = "neo4j"
    fake_config.NEO4J_PASSWORD = "test"
    stubs = {
        "shared.kafka":          fake_kafka,
        "shared.config":         fake_config,
        "shared.database":       mock.MagicMock(),
        "asyncpg":               mock.MagicMock(),
        "sentence_transformers": mock.MagicMock(),
        "qdrant_client":         mock.MagicMock(),
        "qdrant_client.models":  mock.MagicMock(),
    }
    if extra:
        stubs.update(extra)
    return stubs


def _load_app(name, rel_path, extra=None):
    import importlib.util
    # Add service-specific source dir to sys.path so relative imports work
    service_dir = os.path.join(_ROOT, os.path.dirname(rel_path))
    if service_dir not in sys.path:
        sys.path.insert(0, service_dir)
    with mock.patch.dict("sys.modules", _stub_modules(extra)):
        spec = importlib.util.spec_from_file_location(name, os.path.join(_ROOT, rel_path))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


# =============================================================================
# 1. Repository Service — CRUD endpoints
# =============================================================================

class TestRepositoryServiceEndpoints:

    def setup_method(self):
        mod = _load_app("repo_main", "src/repository-service/main.py")
        # Patch out Kafka and DB so routes are pure in-memory
        mod.publisher = mock.AsyncMock()
        mod.publisher.publish = mock.AsyncMock()
        # Import service module and reset its in-memory state
        import importlib
        svc = importlib.import_module("service")
        svc._store     = {}
        svc._url_index = {}
        svc._pool      = None
        self.client = TestClient(mod.app, raise_server_exceptions=True)

    def test_health_check(self):
        r = self.client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_create_repository_returns_201(self):
        r = self.client.post("/repositories", json={
            "name": "my-service", "url": "https://github.com/acme/my-service",
            "language": "Python", "organizationId": "org_1", "createdBy": "user_1",
        })
        assert r.status_code == 201
        data = r.json()["data"]
        assert data["name"] == "my-service"
        assert data["language"] == "Python"

    def test_create_repository_returns_repository_id(self):
        r = self.client.post("/repositories", json={
            "name": "svc", "url": "https://github.com/acme/svc",
            "language": "Go", "organizationId": "org_1", "createdBy": "user_1",
        })
        assert "repositoryId" in r.json()["data"]

    def test_duplicate_url_returns_409(self):
        payload = {
            "name": "svc", "url": "https://github.com/acme/unique",
            "language": "Python", "organizationId": "org_1", "createdBy": "user_1",
        }
        self.client.post("/repositories", json=payload)
        r2 = self.client.post("/repositories", json=payload)
        assert r2.status_code == 409

    def test_get_repository_returns_200(self):
        r_create = self.client.post("/repositories", json={
            "name": "svc", "url": "https://github.com/acme/svc2",
            "language": "Python", "organizationId": "org_1", "createdBy": "user_1",
        })
        repo_id = r_create.json()["data"]["repositoryId"]
        r_get = self.client.get(f"/repositories/{repo_id}")
        assert r_get.status_code == 200
        assert r_get.json()["data"]["repositoryId"] == repo_id

    def test_get_nonexistent_repository_returns_404(self):
        r = self.client.get("/repositories/nonexistent")
        assert r.status_code == 404

    def test_list_repositories_returns_all(self):
        for i in range(3):
            self.client.post("/repositories", json={
                "name": f"svc{i}", "url": f"https://github.com/acme/svc{i}",
                "language": "Python", "organizationId": "org_1", "createdBy": "user_1",
            })
        r = self.client.get("/repositories")
        assert r.json()["total"] == 3

    def test_delete_repository_returns_200(self):
        r_create = self.client.post("/repositories", json={
            "name": "to-delete", "url": "https://github.com/acme/delete-me",
            "language": "Python", "organizationId": "org_1", "createdBy": "user_1",
        })
        repo_id = r_create.json()["data"]["repositoryId"]
        r_del = self.client.delete(f"/repositories/{repo_id}?deletedBy=user_1")
        assert r_del.status_code == 200

    def test_delete_nonexistent_returns_404(self):
        r = self.client.delete("/repositories/ghost?deletedBy=user_1")
        assert r.status_code == 404

    def test_update_repository_changes_name(self):
        r_create = self.client.post("/repositories", json={
            "name": "old-name", "url": "https://github.com/acme/upd",
            "language": "Python", "organizationId": "org_1", "createdBy": "user_1",
        })
        repo_id = r_create.json()["data"]["repositoryId"]
        r_upd = self.client.put(f"/repositories/{repo_id}", json={
            "name": "new-name", "updatedBy": "user_1",
        })
        assert r_upd.status_code == 200
        assert r_upd.json()["data"]["name"] == "new-name"


# =============================================================================
# 2. Search Service — search endpoint
# =============================================================================

class TestSearchServiceEndpoints:

    def setup_method(self):
        mod = _load_app("srch_int", "src/search-service/main.py")
        mod.publisher = mock.AsyncMock()
        mod.publisher.publish = mock.AsyncMock()
        self.client = TestClient(mod.app, raise_server_exceptions=True)
        self.mod = mod

    def test_health_check(self):
        r = self.client.get("/health")
        assert r.status_code == 200

    def test_search_empty_index_returns_empty(self):
        r = self.client.get("/search?q=anything")
        assert r.status_code == 200
        assert r.json()["total"] == 0

    def test_search_finds_indexed_repo(self):
        import asyncio
        asyncio.get_event_loop().run_until_complete(
            self.mod.handle_event("repository.created", {
                "eventType": "RepositoryCreated", "eventId": "e1",
                "organizationId": "org_1",
                "payload": {
                    "repositoryId": "r1", "name": "payment-gateway",
                    "language": "Python", "url": "https://github.com/acme/pg",
                },
            })
        )
        r = self.client.get("/search?q=payment")
        assert r.status_code == 200
        results = r.json()["results"]
        assert any(res["id"] == "r1" for res in results)

    def test_search_finds_dependency_after_event(self):
        import asyncio
        asyncio.get_event_loop().run_until_complete(
            self.mod.handle_event("dependency.detected", {
                "eventType": "DependencyDetected", "eventId": "e2",
                "organizationId": "org_1",
                "payload": {
                    "repositoryId": "r1", "name": "fastapi",
                    "version": "0.110.0", "ecosystem": "pip",
                },
            })
        )
        r = self.client.get("/search?q=fastapi")
        assert r.status_code == 200
        assert r.json()["total"] >= 1

    def test_search_finds_commit_message(self):
        import asyncio
        from datetime import datetime, timezone
        asyncio.get_event_loop().run_until_complete(
            self.mod.handle_event("commit.analyzed", {
                "eventType": "CommitAnalyzed", "eventId": "e3",
                "organizationId": "org_1",
                "payload": {
                    "repositoryId": "r1",
                    "sha": "abc123",
                    "authorEmail": "dev@example.com",
                    "authorName": "Alice",
                    "message": "feat: implement circuit breaker pattern",
                    "filesChanged": [],
                    "committedAt": datetime.now(timezone.utc).isoformat(),
                },
            })
        )
        r = self.client.get("/search?q=circuit+breaker")
        assert r.status_code == 200
        assert r.json()["total"] >= 1

    def test_suggest_endpoint_returns_list(self):
        r = self.client.get("/search/suggest?q=pay&limit=5")
        assert r.status_code == 200
        assert "suggestions" in r.json()


# =============================================================================
# 3. Embedding Service — embedding endpoints
# =============================================================================

class TestEmbeddingServiceEndpoints:

    def setup_method(self):
        mod = _load_app("emb_int", "src/embedding-service/main.py")
        mod._model  = None
        mod._qdrant = None
        mod._embeddings = {}
        mod.publisher = mock.AsyncMock()
        mod.publisher.publish = mock.AsyncMock()
        self.client = TestClient(mod.app, raise_server_exceptions=True)
        self.mod = mod

    def test_health_check(self):
        r = self.client.get("/health")
        assert r.status_code == 200

    def test_list_embeddings_empty(self):
        r = self.client.get("/embeddings")
        assert r.status_code == 200
        assert r.json()["total"] == 0

    def test_get_nonexistent_embedding_404(self):
        r = self.client.get("/embeddings/nonexistent")
        assert r.status_code == 404

    def test_embedding_stored_after_event(self):
        import asyncio
        asyncio.get_event_loop().run_until_complete(
            self.mod.handle_event("document.processed", {
                "eventType": "DocumentProcessed", "eventId": "e1",
                "organizationId": "org_1",
                "payload": {
                    "documentId": "doc_int_1", "repositoryId": "r1",
                    "chunkCount": 1,
                    "chunkPreviews": ["hello world integration test content"],
                },
            })
        )
        r = self.client.get("/embeddings/doc_int_1")
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["documentId"] == "doc_int_1"
        assert data["backend"] == "in-memory"

    def test_include_vectors_returns_vectors(self):
        import asyncio
        asyncio.get_event_loop().run_until_complete(
            self.mod.handle_event("document.processed", {
                "eventType": "DocumentProcessed", "eventId": "e2",
                "organizationId": "org_1",
                "payload": {
                    "documentId": "doc_int_2", "repositoryId": "r1",
                    "chunkCount": 1,
                    "chunkPreviews": ["some content for vector test"],
                },
            })
        )
        r = self.client.get("/embeddings/doc_int_2?includeVectors=true")
        assert r.status_code == 200
        data = r.json()["data"]
        assert "vectors" in data
        assert len(data["vectors"]) == 1
        assert isinstance(data["vectors"][0], list)

    def test_search_similar_503_without_qdrant(self):
        r = self.client.get("/embeddings/search/similar?q=test")
        assert r.status_code == 503

    def test_chunk_previews_produce_multiple_vectors(self):
        import asyncio
        asyncio.get_event_loop().run_until_complete(
            self.mod.handle_event("document.processed", {
                "eventType": "DocumentProcessed", "eventId": "e3",
                "organizationId": "org_1",
                "payload": {
                    "documentId": "doc_int_3", "repositoryId": "r1",
                    "chunkCount": 3,
                    "chunkPreviews": [
                        "first chunk about authentication",
                        "second chunk about authorization",
                        "third chunk about rate limiting",
                    ],
                },
            })
        )
        r = self.client.get("/embeddings/doc_int_3?includeVectors=true")
        assert r.status_code == 200
        assert len(r.json()["data"]["vectors"]) == 3


# =============================================================================
# 4. Graph Service — node endpoints
# =============================================================================

class TestGraphServiceEndpoints:

    def setup_method(self):
        extra = {
            "neo4j":        mock.MagicMock(),
            "neo4j.AsyncGraphDatabase": mock.MagicMock(),
        }
        mod = _load_app("graph_int", "src/graph-service/main.py", extra)
        mod._driver = None
        mod._nodes  = {}
        mod._relationships = []
        mod.publisher = mock.AsyncMock()
        mod.publisher.publish = mock.AsyncMock()
        self.client = TestClient(mod.app, raise_server_exceptions=True)
        self.mod = mod

    def test_health_check(self):
        r = self.client.get("/health")
        assert r.status_code == 200

    def test_list_nodes_empty(self):
        r = self.client.get("/graph/nodes")
        assert r.status_code == 200
        assert r.json()["data"] == []

    def test_node_created_after_repository_created_event(self):
        import asyncio
        asyncio.get_event_loop().run_until_complete(
            self.mod.handle_event("repository.created", {
                "eventType": "RepositoryCreated", "eventId": "e1",
                "organizationId": "org_1",
                "payload": {
                    "repositoryId": "r1", "name": "my-service",
                    "url": "https://github.com/acme/my-service",
                    "language": "Python", "createdBy": "user_1",
                },
            })
        )
        r = self.client.get("/graph/nodes?label=Repository")
        assert r.status_code == 200
        nodes = r.json()["data"]
        assert any(n["nodeId"] == "r1" for n in nodes)

    def test_dependency_node_created_after_dependency_detected_event(self):
        import asyncio
        # First seed a repo node
        asyncio.get_event_loop().run_until_complete(
            self.mod.handle_event("repository.created", {
                "eventType": "RepositoryCreated", "eventId": "e1",
                "organizationId": "org_1",
                "payload": {
                    "repositoryId": "r2", "name": "svc",
                    "url": "https://github.com/acme/svc",
                    "language": "Python", "createdBy": "u1",
                },
            })
        )
        asyncio.get_event_loop().run_until_complete(
            self.mod.handle_event("dependency.detected", {
                "eventType": "DependencyDetected", "eventId": "e2",
                "organizationId": "org_1",
                "payload": {
                    "repositoryId": "r2", "name": "requests",
                    "version": "2.31.0", "ecosystem": "pip",
                    "sourceFile": "requirements.txt",
                },
            })
        )
        r = self.client.get("/graph/nodes?label=Dependency")
        nodes = r.json()["data"]
        assert any(n["properties"].get("name") == "requests" for n in nodes)

    def test_label_filter_returns_correct_type(self):
        import asyncio
        asyncio.get_event_loop().run_until_complete(
            self.mod.handle_event("repository.created", {
                "eventType": "RepositoryCreated", "eventId": "e1",
                "organizationId": "org_1",
                "payload": {
                    "repositoryId": "r3", "name": "api",
                    "url": "https://github.com/acme/api",
                    "language": "Go", "createdBy": "u1",
                },
            })
        )
        r = self.client.get("/graph/nodes?label=Repository")
        for node in r.json()["data"]:
            assert node["label"] == "Repository"

    def test_invalid_label_returns_error(self):
        # FastAPI/Pydantic validates enum-like query params and returns 422;
        # graph service raises ValueError caught as HTTPException 400.
        # Either is acceptable — just not 200.
        r = self.client.get("/graph/nodes?label=INVALID_LABEL")
        assert r.status_code in (400, 422)
