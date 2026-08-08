"""
tests/test_architecture_handler.py

Unit tests for:
  - ArchitectureAnalyzed event handler in graph-service
  - GET /graph/analysis/architecture/{repo_id} endpoint (in-memory)
  - Neo4j architecture analysis (session lifecycle)
"""
import sys, os, types, asyncio, unittest.mock as mock
from datetime import datetime, timezone

_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(_ROOT, "src"))

import pytest

# ---------------------------------------------------------------------------
# Module loaders
# ---------------------------------------------------------------------------

def _load_graph():
    import importlib.util
    stubs = {
        "shared.kafka":  types.SimpleNamespace(
            EventPublisher=mock.MagicMock, EventSubscriber=mock.MagicMock),
        "shared.config": types.SimpleNamespace(
            NEO4J_URI="bolt://x", NEO4J_USER="u", NEO4J_PASSWORD="p"),
        "shared.database": mock.MagicMock(),
        "asyncpg":         mock.MagicMock(),
        "neo4j":           mock.MagicMock(),
    }
    with mock.patch.dict("sys.modules", stubs):
        spec = importlib.util.spec_from_file_location(
            "graph_arch",
            os.path.join(_ROOT, "src/graph-service/main.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


MOD = _load_graph()


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _reset():
    MOD._nodes = {}
    MOD._relationships = []
    MOD._driver = None
    # Publisher must be async-awaitable
    MOD.publisher = mock.MagicMock()
    MOD.publisher.publish = mock.AsyncMock()


def _now():
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Payload builder helpers
# ---------------------------------------------------------------------------

def _sym(name, sym_type="function", file_path="src/app.py", lang="python"):
    return {
        "name": name, "symbolType": sym_type,
        "filePath": file_path, "lineNumber": 10, "language": lang,
    }


def _rel(from_sym, to_sym, rel_type="CALLS", file_path="src/app.py"):
    return {
        "fromSymbol": from_sym, "toSymbol": to_sym,
        "relationType": rel_type, "filePath": file_path,
    }


def _arch_event(repo_id="r1", symbols=None, relations=None):
    return {
        "eventType": "ArchitectureAnalyzed",
        "eventId": "e1",
        "organizationId": "org_1",
        "payload": {
            "repositoryId": repo_id,
            "language": "multi",
            "symbols": symbols or [],
            "relations": relations or [],
            "filesAnalyzed": 5,
            "analyzedAt": _now(),
        },
    }


# =============================================================================
# ArchitectureAnalyzed — event handler (in-memory)
# =============================================================================

class TestArchitectureAnalyzedHandler:

    def setup_method(self):
        _reset()
        # Ensure a parent repo node exists for BELONGS_TO links
        MOD._nodes["r1"] = {
            "nodeId": "r1", "label": "Repository",
            "properties": {"name": "test-repo"},
            "createdAt": _now(), "updatedAt": _now(),
        }

    def test_function_node_created(self):
        run(MOD.handle_event("architecture.analyzed", _arch_event(
            symbols=[_sym("process_payment", "function")]
        )))
        node_ids = list(MOD._nodes.keys())
        assert any("process_payment" in nid for nid in node_ids)

    def test_class_node_created(self):
        run(MOD.handle_event("architecture.analyzed", _arch_event(
            symbols=[_sym("PaymentService", "class")]
        )))
        node_ids = list(MOD._nodes.keys())
        assert any("PaymentService" in nid for nid in node_ids)

    def test_function_node_has_correct_label(self):
        run(MOD.handle_event("architecture.analyzed", _arch_event(
            symbols=[_sym("my_func", "function")]
        )))
        fn_nodes = [n for n in MOD._nodes.values() if n["label"] == "Function"]
        assert len(fn_nodes) >= 1

    def test_class_node_has_correct_label(self):
        run(MOD.handle_event("architecture.analyzed", _arch_event(
            symbols=[_sym("MyClass", "class")]
        )))
        cls_nodes = [n for n in MOD._nodes.values() if n["label"] == "Class"]
        assert len(cls_nodes) >= 1

    def test_belongs_to_relationship_created(self):
        run(MOD.handle_event("architecture.analyzed", _arch_event(
            symbols=[_sym("do_work", "function")]
        )))
        rels = [r for r in MOD._relationships if r["type"] == "BELONGS_TO"]
        assert len(rels) >= 1

    def test_multiple_symbols_all_created(self):
        run(MOD.handle_event("architecture.analyzed", _arch_event(
            symbols=[
                _sym("alpha", "function"),
                _sym("beta",  "function"),
                _sym("Gamma", "class"),
            ]
        )))
        fn_nodes  = [n for n in MOD._nodes.values() if n["label"] == "Function"]
        cls_nodes = [n for n in MOD._nodes.values() if n["label"] == "Class"]
        assert len(fn_nodes) >= 2
        assert len(cls_nodes) >= 1

    def test_calls_relation_created_when_both_nodes_exist(self):
        # Pre-insert both nodes so the relationship check passes
        MOD._nodes["r1:src/app.py:caller"] = {
            "nodeId": "r1:src/app.py:caller", "label": "Function",
            "properties": {"name": "caller", "repositoryId": "r1"},
            "createdAt": _now(), "updatedAt": _now(),
        }
        MOD._nodes["r1:src/app.py:callee"] = {
            "nodeId": "r1:src/app.py:callee", "label": "Function",
            "properties": {"name": "callee", "repositoryId": "r1"},
            "createdAt": _now(), "updatedAt": _now(),
        }
        run(MOD.handle_event("architecture.analyzed", _arch_event(
            symbols=[],
            relations=[_rel("caller", "callee", "CALLS")],
        )))
        calls = [r for r in MOD._relationships if r["type"] == "CALLS"]
        assert len(calls) >= 1

    def test_implements_relation_created_when_both_nodes_exist(self):
        MOD._nodes["r1:src/app.py:Child"] = {
            "nodeId": "r1:src/app.py:Child", "label": "Class",
            "properties": {"name": "Child", "repositoryId": "r1"},
            "createdAt": _now(), "updatedAt": _now(),
        }
        MOD._nodes["r1:src/app.py:Parent"] = {
            "nodeId": "r1:src/app.py:Parent", "label": "Class",
            "properties": {"name": "Parent", "repositoryId": "r1"},
            "createdAt": _now(), "updatedAt": _now(),
        }
        run(MOD.handle_event("architecture.analyzed", _arch_event(
            symbols=[],
            relations=[_rel("Child", "Parent", "IMPLEMENTS")],
        )))
        impls = [r for r in MOD._relationships if r["type"] == "IMPLEMENTS"]
        assert len(impls) >= 1

    def test_calls_relation_skipped_when_from_node_missing(self):
        # Only 'callee' exists — 'caller' does not
        MOD._nodes["r1:src/app.py:callee"] = {
            "nodeId": "r1:src/app.py:callee", "label": "Function",
            "properties": {"name": "callee", "repositoryId": "r1"},
            "createdAt": _now(), "updatedAt": _now(),
        }
        run(MOD.handle_event("architecture.analyzed", _arch_event(
            symbols=[],
            relations=[_rel("ghost_caller", "callee", "CALLS")],
        )))
        calls = [r for r in MOD._relationships if r["type"] == "CALLS"]
        assert calls == []

    def test_empty_payload_does_not_crash(self):
        # Should handle gracefully with no symbols/relations
        run(MOD.handle_event("architecture.analyzed", _arch_event(
            symbols=[], relations=[]
        )))
        # No crash == pass

    def test_symbol_properties_stored(self):
        run(MOD.handle_event("architecture.analyzed", _arch_event(
            symbols=[_sym("my_func", "function", "src/service.py", "python")]
        )))
        fn_nodes = [n for n in MOD._nodes.values() if n["label"] == "Function"]
        assert fn_nodes
        props = fn_nodes[0]["properties"]
        assert props.get("name") == "my_func"
        assert props.get("language") == "python"
        assert "src/service.py" in props.get("filePath", "")


# =============================================================================
# /graph/analysis/architecture/{repo_id} — in-memory endpoint
# =============================================================================

class TestArchitectureEndpoint:

    def setup_method(self):
        _reset()
        MOD._driver = None

    def _add_repo(self, repo_id="r1"):
        MOD._nodes[repo_id] = {
            "nodeId": repo_id, "label": "Repository",
            "properties": {"name": "test"},
            "createdAt": _now(), "updatedAt": _now(),
        }

    def _add_func(self, node_id, name, repo_id="r1", lang="python"):
        MOD._nodes[node_id] = {
            "nodeId": node_id, "label": "Function",
            "properties": {"name": name, "repositoryId": repo_id,
                           "language": lang, "filePath": "src/app.py"},
            "createdAt": _now(), "updatedAt": _now(),
        }

    def _add_cls(self, node_id, name, repo_id="r1"):
        MOD._nodes[node_id] = {
            "nodeId": node_id, "label": "Class",
            "properties": {"name": name, "repositoryId": repo_id,
                           "language": "python", "filePath": "src/app.py"},
            "createdAt": _now(), "updatedAt": _now(),
        }

    def test_404_for_unknown_repo(self):
        from fastapi.testclient import TestClient
        client = TestClient(MOD.app)
        r = client.get("/graph/analysis/architecture/ghost")
        assert r.status_code == 404

    def test_returns_zero_counts_for_repo_with_no_symbols(self):
        from fastapi.testclient import TestClient
        self._add_repo("r1")
        client = TestClient(MOD.app)
        r = client.get("/graph/analysis/architecture/r1")
        assert r.status_code == 200
        data = r.json()
        assert data["totalSymbols"] == 0
        assert data["totalFunctions"] == 0
        assert data["totalClasses"] == 0

    def test_counts_functions_correctly(self):
        from fastapi.testclient import TestClient
        self._add_repo("r1")
        self._add_func("r1:f1", "foo"); self._add_func("r1:f2", "bar")
        client = TestClient(MOD.app)
        r = client.get("/graph/analysis/architecture/r1")
        data = r.json()
        assert data["totalFunctions"] == 2
        assert data["totalClasses"] == 0
        assert data["totalSymbols"] == 2

    def test_counts_classes_correctly(self):
        from fastapi.testclient import TestClient
        self._add_repo("r1")
        self._add_cls("r1:c1", "Svc")
        client = TestClient(MOD.app)
        r = client.get("/graph/analysis/architecture/r1")
        data = r.json()
        assert data["totalClasses"] == 1

    def test_language_breakdown_present(self):
        from fastapi.testclient import TestClient
        self._add_repo("r1")
        self._add_func("r1:f1", "foo", lang="python")
        self._add_func("r1:f2", "bar", lang="javascript")
        client = TestClient(MOD.app)
        r = client.get("/graph/analysis/architecture/r1")
        data = r.json()
        assert "languageBreakdown" in data
        assert data["languageBreakdown"].get("python", 0) >= 1
        assert data["languageBreakdown"].get("javascript", 0) >= 1

    def test_top_called_functions_sorted_by_in_degree(self):
        from fastapi.testclient import TestClient
        self._add_repo("r1")
        self._add_func("r1:popular", "popular")
        self._add_func("r1:rare",    "rare")
        # popular is called twice, rare never
        MOD._relationships.extend([
            {"relationshipId": "x1", "from": "a", "to": "r1:popular",
             "type": "CALLS", "properties": {}, "createdAt": _now()},
            {"relationshipId": "x2", "from": "b", "to": "r1:popular",
             "type": "CALLS", "properties": {}, "createdAt": _now()},
        ])
        client = TestClient(MOD.app)
        r = client.get("/graph/analysis/architecture/r1")
        top = r.json()["topCalledFunctions"]
        assert top[0]["name"] == "popular"

    def test_response_has_all_required_keys(self):
        from fastapi.testclient import TestClient
        self._add_repo("r1")
        client = TestClient(MOD.app)
        r = client.get("/graph/analysis/architecture/r1")
        data = r.json()
        for key in ("repositoryId", "totalFunctions", "totalClasses",
                    "totalSymbols", "languageBreakdown",
                    "topCalledFunctions", "circularCalls", "analyzedAt"):
            assert key in data, f"Missing key: {key}"

    def test_only_counts_symbols_for_requested_repo(self):
        from fastapi.testclient import TestClient
        # Two repos — only r1 should be counted for r1 query
        self._add_repo("r1"); self._add_repo("r2")
        self._add_func("r1:f1", "r1_func", repo_id="r1")
        self._add_func("r2:f1", "r2_func", repo_id="r2")
        client = TestClient(MOD.app)
        r = client.get("/graph/analysis/architecture/r1")
        data = r.json()
        assert data["totalFunctions"] == 1


# =============================================================================
# Shared models — ArchitectureAnalyzedPayload
# =============================================================================

class TestArchitecturePayloadModel:

    def test_payload_creation(self):
        from shared.models import ArchitectureAnalyzedPayload, CodeSymbol, CodeRelation
        sym = CodeSymbol(name="foo", symbolType="function",
                         filePath="src/a.py", lineNumber=1, language="python")
        rel = CodeRelation(fromSymbol="foo", toSymbol="bar",
                           relationType="CALLS", filePath="src/a.py")
        p = ArchitectureAnalyzedPayload(
            repositoryId="r1", symbols=[sym], relations=[rel], filesAnalyzed=1
        )
        assert p.repository_id == "r1"
        assert len(p.symbols) == 1
        assert len(p.relations) == 1

    def test_code_symbol_aliases(self):
        from shared.models import CodeSymbol
        s = CodeSymbol(symbolType="class", name="Foo",
                       filePath="a.py", lineNumber=5, language="python")
        data = s.model_dump(by_alias=True)
        assert "symbolType" in data
        assert "filePath" in data
        assert "lineNumber" in data

    def test_code_relation_aliases(self):
        from shared.models import CodeRelation
        r = CodeRelation(fromSymbol="a", toSymbol="b",
                         relationType="IMPLEMENTS", filePath="x.py")
        data = r.model_dump(by_alias=True)
        assert "fromSymbol" in data
        assert "toSymbol" in data
        assert "relationType" in data

    def test_default_language_python(self):
        from shared.models import CodeSymbol
        s = CodeSymbol(name="f", symbolType="function", filePath="a.py")
        assert s.language == "python"

    def test_event_serialization(self):
        from shared.models import ArchitectureAnalyzedPayload, create_event
        p = ArchitectureAnalyzedPayload(repositoryId="r1", filesAnalyzed=3)
        env = create_event("ArchitectureAnalyzed", "r1", "org_1", p)
        data = env.model_dump(by_alias=True)
        assert data["eventType"] == "ArchitectureAnalyzed"
        assert data["payload"]["repositoryId"] == "r1"

