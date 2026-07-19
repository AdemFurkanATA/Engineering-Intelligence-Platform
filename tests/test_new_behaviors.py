"""
tests/test_new_behaviors.py

Targeted unit tests for Phase 1 new behaviours that lacked coverage:

  1. Graph service   — in-memory RepositoryUpdated handler (changedFields)
  2. Embedding service — in-memory vector preservation
                       — textPreview chunk splitting logic
                       — stub embedder determinism and unit-vector property
  3. Search service  — RepositoryUpdated re-index preserves existing tokens
                    — DocumentProcessed textPreview vs metadata fallback
  4. Shared models   — changedFields and textPreview new payload fields

Run with:
  PYTHONPATH=src python -m pytest tests/test_new_behaviors.py -v
"""
import asyncio
import math
import sys
import os
import types
import unittest.mock as mock

# ── path setup ───────────────────────────────────────────────────────────────
_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(_ROOT, "src"))
sys.path.insert(0, os.path.join(_ROOT, "src", "search-service"))

import pytest


# =============================================================================
# helpers
# =============================================================================

def _make_fake_mods():
    """Return a dict of minimal stub modules shared across helper factories."""
    fake_kafka = types.ModuleType("shared.kafka")
    fake_kafka.EventPublisher  = mock.MagicMock
    fake_kafka.EventSubscriber = mock.MagicMock
    fake_config = types.ModuleType("shared.config")
    fake_config.QDRANT_URL     = "http://localhost:6333"
    fake_config.NEO4J_URI      = "bolt://localhost:7687"
    fake_config.NEO4J_USER     = "neo4j"
    fake_config.NEO4J_PASSWORD = "test"
    fake_models = types.ModuleType("shared.models")
    fake_models.EmbeddingGeneratedPayload = mock.MagicMock
    fake_models.GraphUpdatedPayload       = mock.MagicMock
    fake_models.create_event              = mock.MagicMock(return_value=mock.MagicMock())
    return {
        "shared.kafka":          fake_kafka,
        "shared.config":         fake_config,
        "shared.models":         fake_models,
        "sentence_transformers": mock.MagicMock(),
        "qdrant_client":         mock.MagicMock(),
        "qdrant_client.models":  mock.MagicMock(),
        "fastapi":               mock.MagicMock(),
    }


def _load_module(name, rel_path, extra_sys_modules=None):
    import importlib.util
    stubs = _make_fake_mods()
    if extra_sys_modules:
        stubs.update(extra_sys_modules)
    with mock.patch.dict("sys.modules", stubs):
        spec = importlib.util.spec_from_file_location(
            name, os.path.join(_ROOT, rel_path)
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# =============================================================================
# 1. Graph service — in-memory RepositoryUpdated handler
# =============================================================================

class TestGraphInMemoryRepositoryUpdated:

    def setup_method(self):
        mod = _load_module("graph_main", "src/graph-service/main.py")
        mod._driver = None           # force in-memory path
        mod._nodes  = {}
        mod._relationships = []
        mod.publisher = mock.AsyncMock()
        mod.publisher.publish = mock.AsyncMock()
        self.mod = mod

    def _seed(self, repo_id, props):
        self.mod._nodes[repo_id] = {
            "nodeId": repo_id, "label": "Repository",
            "properties": dict(props),
            "createdAt": "2024-01-01T00:00:00Z",
            "updatedAt": "2024-01-01T00:00:00Z",
        }

    def _make_event(self, repo_id, changed_fields):
        return {
            "eventType": "RepositoryUpdated", "eventId": "evt",
            "organizationId": "org_1",
            "payload": {
                "repositoryId":  repo_id,
                "changes":       list(changed_fields.keys()),
                "changedFields": changed_fields,
                "updatedBy":     "user_1",
            },
        }

    def test_patches_changed_property(self):
        self._seed("r1", {"name": "old", "visibility": "private", "language": "Python"})
        _run(self.mod.handle_event("repository.updated", self._make_event("r1", {"name": "new-name"})))
        assert self.mod._nodes["r1"]["properties"]["name"] == "new-name"

    def test_unchanged_properties_preserved(self):
        self._seed("r1", {"name": "old", "visibility": "private", "language": "Python"})
        _run(self.mod.handle_event("repository.updated", self._make_event("r1", {"name": "new-name"})))
        props = self.mod._nodes["r1"]["properties"]
        assert props["visibility"] == "private"
        assert props["language"]   == "Python"

    def test_multiple_fields_updated_at_once(self):
        self._seed("r1", {"name": "old", "visibility": "private"})
        _run(self.mod.handle_event("repository.updated",
             self._make_event("r1", {"name": "updated", "visibility": "public"})))
        props = self.mod._nodes["r1"]["properties"]
        assert props["name"]       == "updated"
        assert props["visibility"] == "public"

    def test_empty_changed_fields_is_noop(self):
        self._seed("r2", {"name": "stable"})
        _run(self.mod.handle_event("repository.updated", self._make_event("r2", {})))
        assert self.mod._nodes["r2"]["properties"]["name"] == "stable"

    def test_unknown_node_does_not_crash(self):
        # Should complete without exception even if node doesn't exist
        _run(self.mod.handle_event("repository.updated",
             self._make_event("nonexistent", {"name": "x"})))


# =============================================================================
# 2. Embedding service — in-memory vector preservation
# =============================================================================

class TestEmbeddingInMemoryVectorPreservation:

    def setup_method(self):
        mod = _load_module("emb_main", "src/embedding-service/main.py")
        mod._model  = None   # stub path
        mod._qdrant = None   # in-memory path
        mod._embeddings = {}
        mod.publisher = mock.AsyncMock()
        mod.publisher.publish = mock.AsyncMock()
        self.mod = mod

    def _make_event(self, doc_id, chunk_count, text_preview=""):
        payload = {"documentId": doc_id, "repositoryId": "r1",
                   "chunkCount": chunk_count}
        if text_preview:
            payload["textPreview"] = text_preview
        return {"eventType": "DocumentProcessed", "eventId": "e",
                "organizationId": "org_1", "payload": payload}

    def test_vectors_key_exists_after_handle_event(self):
        _run(self.mod.handle_event("document.processed",
             self._make_event("d1", 1, "hello world test")))
        assert "vectors" in self.mod._embeddings["d1"]

    def test_vectors_are_non_empty_list_of_lists(self):
        _run(self.mod.handle_event("document.processed",
             self._make_event("d1", 2, "alpha beta gamma delta epsilon")))
        vecs = self.mod._embeddings["d1"]["vectors"]
        assert len(vecs) == 2
        assert isinstance(vecs[0], list)
        assert all(isinstance(x, float) for x in vecs[0])

    def test_backend_field_is_in_memory(self):
        _run(self.mod.handle_event("document.processed",
             self._make_event("d2", 1, "test")))
        assert self.mod._embeddings["d2"]["backend"] == "in-memory"

    def test_chunk_count_matches_requested(self):
        _run(self.mod.handle_event("document.processed",
             self._make_event("d3", 3, " ".join(f"word{i}" for i in range(30)))))
        assert self.mod._embeddings["d3"]["chunkCount"] == 3

    def test_non_document_event_ignored(self):
        _run(self.mod.handle_event("repository.created",
             {"eventType": "RepositoryCreated", "eventId": "e",
              "organizationId": "org_1", "payload": {}}))
        assert len(self.mod._embeddings) == 0


# =============================================================================
# 3. Embedding service — stub embedder properties
# =============================================================================

class TestEmbeddingStubEmbedder:

    def setup_method(self):
        self.mod = _load_module("emb_stub", "src/embedding-service/main.py")

    def test_stub_vector_is_unit_length(self):
        v = self.mod._stub_embed("any text")
        norm = math.sqrt(sum(x * x for x in v))
        assert abs(norm - 1.0) < 1e-6

    def test_stub_vector_correct_dimension(self):
        v = self.mod._stub_embed("text")
        assert len(v) == self.mod.STUB_DIM

    def test_stub_is_deterministic(self):
        assert self.mod._stub_embed("hello") == self.mod._stub_embed("hello")

    def test_stub_different_inputs_differ(self):
        assert self.mod._stub_embed("hello") != self.mod._stub_embed("world")


# =============================================================================
# 4. Embedding service — textPreview chunk splitting
# =============================================================================

class TestEmbeddingTextPreviewChunkSplitting:

    def setup_method(self):
        mod = _load_module("emb_chunk", "src/embedding-service/main.py")
        mod._model  = None
        mod._qdrant = None
        mod._embeddings = {}
        mod.publisher = mock.AsyncMock()
        mod.publisher.publish = mock.AsyncMock()
        self.mod = mod

    def test_single_chunk_produces_one_vector(self):
        preview = " ".join(f"w{i}" for i in range(20))
        event = {"eventType": "DocumentProcessed", "eventId": "e",
                 "organizationId": "o",
                 "payload": {"documentId": "d1", "repositoryId": "r",
                             "chunkCount": 1, "textPreview": preview}}
        _run(self.mod.handle_event("document.processed", event))
        assert len(self.mod._embeddings["d1"]["vectors"]) == 1

    def test_three_chunks_produce_three_vectors(self):
        preview = " ".join(f"token{i}" for i in range(90))
        event = {"eventType": "DocumentProcessed", "eventId": "e",
                 "organizationId": "o",
                 "payload": {"documentId": "d2", "repositoryId": "r",
                             "chunkCount": 3, "textPreview": preview}}
        _run(self.mod.handle_event("document.processed", event))
        assert len(self.mod._embeddings["d2"]["vectors"]) == 3

    def test_missing_preview_still_produces_vectors(self):
        event = {"eventType": "DocumentProcessed", "eventId": "e",
                 "organizationId": "o",
                 "payload": {"documentId": "d3", "repositoryId": "r",
                             "chunkCount": 2}}   # no textPreview
        _run(self.mod.handle_event("document.processed", event))
        assert len(self.mod._embeddings["d3"]["vectors"]) == 2

    def test_vector_dimension_is_stub_dim(self):
        event = {"eventType": "DocumentProcessed", "eventId": "e",
                 "organizationId": "o",
                 "payload": {"documentId": "d4", "repositoryId": "r",
                             "chunkCount": 1, "textPreview": "hello world"}}
        _run(self.mod.handle_event("document.processed", event))
        assert len(self.mod._embeddings["d4"]["vectors"][0]) == self.mod.STUB_DIM


# =============================================================================
# 5. Search service — RepositoryUpdated re-index preserves existing tokens
# =============================================================================

class TestSearchRepositoryUpdatedMerge:

    def setup_method(self):
        mod = _load_module("srch_main", "src/search-service/main.py",
                           {"engine": mock.MagicMock()})
        # Re-import real engine
        mod = _load_module("srch_main2", "src/search-service/main.py")
        mod.publisher = mock.AsyncMock()
        mod.publisher.publish = mock.AsyncMock()
        self.mod = mod

    def test_visibility_change_preserves_name_tokens(self):
        loop = asyncio.get_event_loop()

        # Index via RepositoryCreated
        loop.run_until_complete(self.mod.handle_event("repository.created", {
            "eventType": "RepositoryCreated", "eventId": "e1",
            "organizationId": "org_1",
            "payload": {"repositoryId": "r1", "name": "payment-gateway",
                        "language": "Python", "url": "https://github.com/a/pg"},
        }))

        # Name must be searchable before update
        before = loop.run_until_complete(self.mod.engine.search("payment", top_k=5))
        assert any(r["id"] == "r1" for r in before)

        # Update only visibility
        loop.run_until_complete(self.mod.handle_event("repository.updated", {
            "eventType": "RepositoryUpdated", "eventId": "e2",
            "organizationId": "org_1",
            "payload": {"repositoryId": "r1", "changes": ["visibility"],
                        "changedFields": {"visibility": "public"}, "updatedBy": "u"},
        }))

        # Name still searchable
        after = loop.run_until_complete(self.mod.engine.search("payment", top_k=5))
        assert any(r["id"] == "r1" for r in after), \
            "name token lost after visibility-only update"

        # New value also indexed
        pub_results = loop.run_until_complete(self.mod.engine.search("public", top_k=5))
        assert any(r["id"] == "r1" for r in pub_results)

    def test_no_changed_fields_is_silent_noop(self):
        _run(self.mod.handle_event("repository.updated", {
            "eventType": "RepositoryUpdated", "eventId": "e_noop",
            "organizationId": "org_1",
            "payload": {"repositoryId": "r_missing", "changes": [],
                        "changedFields": {}, "updatedBy": "u"},
        }))  # must not raise


# =============================================================================
# 6. Search service — DocumentProcessed textPreview vs metadata fallback
# =============================================================================

class TestSearchDocumentProcessedIndexing:

    def setup_method(self):
        mod = _load_module("srch_doc", "src/search-service/main.py")
        mod.publisher = mock.AsyncMock()
        mod.publisher.publish = mock.AsyncMock()
        self.mod = mod

    def test_document_indexed_by_text_preview_word(self):
        _run(self.mod.handle_event("document.processed", {
            "eventType": "DocumentProcessed", "eventId": "e1",
            "organizationId": "org_1",
            "payload": {"documentId": "d1", "repositoryId": "r1",
                        "documentType": "MARKDOWN", "fileName": "notes.md",
                        "chunkCount": 1, "wordCount": 10,
                        "textPreview": "kubernetes autoscaling ingress"},
        }))
        results = _run(self.mod.engine.search("kubernetes", top_k=5))
        assert any(r["id"] == "d1" for r in results)

    def test_document_indexed_by_filename_when_no_preview(self):
        _run(self.mod.handle_event("document.processed", {
            "eventType": "DocumentProcessed", "eventId": "e2",
            "organizationId": "org_1",
            "payload": {"documentId": "d2", "repositoryId": "r1",
                        "documentType": "OPENAPI", "fileName": "openapi-spec",
                        "chunkCount": 1, "wordCount": 5},
            # No textPreview
        }))
        results = _run(self.mod.engine.search("openapi", top_k=5))
        assert any(r["id"] == "d2" for r in results)

    def test_preview_word_not_in_filename_still_searchable(self):
        _run(self.mod.handle_event("document.processed", {
            "eventType": "DocumentProcessed", "eventId": "e3",
            "organizationId": "org_1",
            "payload": {"documentId": "d3", "repositoryId": "r1",
                        "documentType": "MARKDOWN", "fileName": "notes",
                        "chunkCount": 1, "wordCount": 5,
                        "textPreview": "circuit breaker resilience"},
        }))
        results = _run(self.mod.engine.search("resilience", top_k=5))
        assert any(r["id"] == "d3" for r in results)


# =============================================================================
# 7. Shared models — new payload fields
# =============================================================================

class TestNewPayloadFields:

    def test_repository_updated_payload_changed_fields_roundtrip(self):
        from shared.models import RepositoryUpdatedPayload
        p = RepositoryUpdatedPayload(
            repositoryId="r1", changes=["name", "visibility"],
            changedFields={"name": "new", "visibility": "public"},
            updatedBy="u1",
        )
        assert p.changed_fields == {"name": "new", "visibility": "public"}

    def test_repository_updated_payload_changed_fields_defaults_empty(self):
        from shared.models import RepositoryUpdatedPayload
        p = RepositoryUpdatedPayload(repositoryId="r1", changes=[], updatedBy="u1")
        assert p.changed_fields == {}

    def test_document_processed_payload_text_preview_roundtrip(self):
        from shared.models import DocumentProcessedPayload
        p = DocumentProcessedPayload(
            documentId="d1", repositoryId="r1", documentType="MD",
            fileName="f.md", chunkCount=1, wordCount=5,
            textPreview="hello world",
        )
        assert p.text_preview == "hello world"

    def test_document_processed_payload_text_preview_defaults_empty(self):
        from shared.models import DocumentProcessedPayload
        p = DocumentProcessedPayload(
            documentId="d1", repositoryId="r1", documentType="MD",
            fileName="f.md", chunkCount=1, wordCount=5,
        )
        assert p.text_preview == ""

    def test_document_processed_event_serialization_includes_text_preview(self):
        from shared.models import DocumentProcessedPayload, create_event
        p = DocumentProcessedPayload(
            documentId="d1", repositoryId="r1", documentType="MD",
            fileName="f.md", chunkCount=2, wordCount=50,
            textPreview="kubernetes deployment",
        )
        env = create_event("DocumentProcessed", "d1", "org_1", p)
        data = env.model_dump(by_alias=True)
        assert data["payload"]["textPreview"] == "kubernetes deployment"

    def test_repository_updated_event_serialization_includes_changed_fields(self):
        from shared.models import RepositoryUpdatedPayload, create_event
        p = RepositoryUpdatedPayload(
            repositoryId="r1", changes=["name"],
            changedFields={"name": "updated-name"}, updatedBy="u1",
        )
        env = create_event("RepositoryUpdated", "r1", "org_1", p)
        data = env.model_dump(by_alias=True)
        assert data["payload"]["changedFields"] == {"name": "updated-name"}
