"""
tests/test_neo4j_path.py

Unit tests for the Neo4j code paths in graph-service.

Strategy
--------
Every Neo4j helper opens `_driver.session()` as an async context manager.
We patch `_driver` with a fake that intercepts these calls and returns
pre-canned data, then assert on:
  - The Cypher queries that were fired (keyword / structure checks)
  - The session lifecycle (every __aenter__ paired with __aexit__ == no leaks)
  - The returned data structures

Run with:
  PYTHONPATH=src python -m pytest tests/test_neo4j_path.py -v
"""
import sys, os, types, asyncio, unittest.mock as mock
from datetime import datetime, timezone, timedelta

_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(_ROOT, "src"))

import pytest

# ---------------------------------------------------------------------------
# Module loader
# ---------------------------------------------------------------------------

def _load_graph_module():
    import importlib.util
    stubs = {
        "shared.kafka":  types.SimpleNamespace(
            EventPublisher=mock.MagicMock, EventSubscriber=mock.MagicMock),
        "shared.config": types.SimpleNamespace(
            NEO4J_URI="bolt://localhost:7687",
            NEO4J_USER="neo4j",
            NEO4J_PASSWORD="test"),
        "shared.database": mock.MagicMock(),
        "asyncpg":         mock.MagicMock(),
        "neo4j":           mock.MagicMock(),
    }
    with mock.patch.dict("sys.modules", stubs):
        spec = importlib.util.spec_from_file_location(
            "graph_neo4j_path",
            os.path.join(_ROOT, "src/graph-service/main.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


MOD = _load_graph_module()


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Mock session / driver infrastructure
# ---------------------------------------------------------------------------

class _MockResult:
    """Simulates an AsyncResult returned by session.run()."""
    def __init__(self, rows, single_row=None):
        self._rows = rows
        self._single = single_row

    async def data(self):
        return list(self._rows)

    async def single(self):
        if self._single is not None:
            return self._single
        return self._rows[0] if self._rows else None


class _MockSession:
    """Tracks run() calls and context-manager open/close count."""
    def __init__(self, result_queue, tracker):
        self._queue = result_queue
        self._tracker = tracker
        self.queries = []

    async def run(self, query, **kwargs):
        self.queries.append(query.strip())
        return self._queue.pop(0) if self._queue else _MockResult([])

    async def __aenter__(self):
        self._tracker.append("open")
        return self

    async def __aexit__(self, *_):
        self._tracker.append("close")


class _MockDriver:
    def __init__(self, result_queue, tracker):
        self._queue = result_queue
        self._tracker = tracker
        self._sessions = []

    def session(self):
        s = _MockSession(self._queue, self._tracker)
        self._sessions.append(s)
        return s

    @property
    def all_queries(self):
        return [q for s in self._sessions for q in s.queries]


def make_driver(*row_lists, single_rows=None):
    """
    Each positional arg is a list-of-dicts for one session.run().data() call.
    single_rows is a parallel list for .single() calls (None = use first row).
    """
    tracker = []
    singles = list(single_rows or [None] * len(row_lists))
    queue = [_MockResult(rows, singles[i] if i < len(singles) else None)
             for i, rows in enumerate(row_lists)]
    return _MockDriver(queue, tracker), tracker


def _iso(days_ago=0):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


# =============================================================================
# _neo4j_upsert_node
# =============================================================================

class TestNeo4jUpsertNode:

    def _row(self, node_id="r1"):
        return {"n": {"nodeId": node_id, "name": "svc",
                      "createdAt": _iso(), "updatedAt": _iso()}}

    def test_returns_node_dict(self):
        row = self._row()
        drv, _ = make_driver([row], single_rows=[row])
        MOD._driver = drv
        result = run(MOD._neo4j_upsert_node("r1", "Repository", {"name": "svc"}))
        assert result["nodeId"] == "r1"
        assert result["label"] == "Repository"

    def test_session_no_leak(self):
        row = self._row()
        drv, tracker = make_driver([row], single_rows=[row])
        MOD._driver = drv
        run(MOD._neo4j_upsert_node("r1", "Repository", {"name": "svc"}))
        assert tracker.count("open") == tracker.count("close")

    def test_cypher_uses_merge(self):
        row = self._row()
        drv, _ = make_driver([row], single_rows=[row])
        MOD._driver = drv
        run(MOD._neo4j_upsert_node("r1", "Repository", {"name": "svc"}))
        assert any("MERGE" in q for q in drv.all_queries)

    def test_none_properties_do_not_crash(self):
        row = self._row()
        drv, tracker = make_driver([row], single_rows=[row])
        MOD._driver = drv
        run(MOD._neo4j_upsert_node("r1", "Repository", {"name": "svc", "lang": None}))
        assert tracker.count("open") == tracker.count("close")


# =============================================================================
# _neo4j_get_node
# =============================================================================

class TestNeo4jGetNode:

    def test_returns_none_when_missing(self):
        drv, _ = make_driver([], single_rows=[None])
        MOD._driver = drv
        assert run(MOD._neo4j_get_node("ghost")) is None

    def test_returns_dict_when_found(self):
        row = {"n": {"nodeId": "r1", "name": "svc",
                     "createdAt": _iso(), "updatedAt": _iso()},
               "labels": ["Repository"]}
        drv, _ = make_driver([row], single_rows=[row])
        MOD._driver = drv
        result = run(MOD._neo4j_get_node("r1"))
        assert result["nodeId"] == "r1"
        assert result["label"] == "Repository"

    def test_session_no_leak_found(self):
        row = {"n": {"nodeId": "r1", "createdAt": _iso(), "updatedAt": _iso()},
               "labels": ["Repository"]}
        drv, tracker = make_driver([row], single_rows=[row])
        MOD._driver = drv
        run(MOD._neo4j_get_node("r1"))
        assert tracker.count("open") == tracker.count("close")

    def test_session_no_leak_not_found(self):
        drv, tracker = make_driver([], single_rows=[None])
        MOD._driver = drv
        run(MOD._neo4j_get_node("ghost"))
        assert tracker.count("open") == tracker.count("close")

    def test_label_extracted_correctly(self):
        row = {"n": {"nodeId": "d1", "createdAt": _iso(), "updatedAt": _iso()},
               "labels": ["Dependency"]}
        drv, _ = make_driver([row], single_rows=[row])
        MOD._driver = drv
        assert run(MOD._neo4j_get_node("d1"))["label"] == "Dependency"


# =============================================================================
# _neo4j_list_nodes
# =============================================================================

class TestNeo4jListNodes:

    def _row(self, node_id, label):
        return {"n": {"nodeId": node_id, "createdAt": _iso(), "updatedAt": _iso()},
                "lbls": [label]}

    def test_empty_graph_returns_empty_list(self):
        drv, _ = make_driver([])
        MOD._driver = drv
        assert run(MOD._neo4j_list_nodes(None)) == []

    def test_session_no_leak(self):
        drv, tracker = make_driver([])
        MOD._driver = drv
        run(MOD._neo4j_list_nodes(None))
        assert tracker.count("open") == tracker.count("close")

    def test_all_nodes_returned_without_filter(self):
        rows = [self._row("r1", "Repository"), self._row("d1", "Dependency")]
        drv, _ = make_driver(rows)
        MOD._driver = drv
        assert len(run(MOD._neo4j_list_nodes(None))) == 2

    def test_label_filter_backtick_quoted(self):
        drv, _ = make_driver([])
        MOD._driver = drv
        run(MOD._neo4j_list_nodes("Repository"))
        assert any("`Repository`" in q for q in drv.all_queries)

    def test_unknown_label_raises_value_error(self):
        drv, _ = make_driver([])
        MOD._driver = drv
        with pytest.raises(ValueError, match="Unknown label"):
            run(MOD._neo4j_list_nodes("BAD_LABEL"))

    def test_node_id_in_result(self):
        drv, _ = make_driver([self._row("r99", "Repository")])
        MOD._driver = drv
        result = run(MOD._neo4j_list_nodes("Repository"))
        assert result[0]["nodeId"] == "r99"


# =============================================================================
# _neo4j_add_relationship
# =============================================================================

class TestNeo4jAddRelationship:

    def _row(self):
        return {"relId": "rel-uuid-123", "createdAt": _iso()}

    def test_returns_relationship_dict(self):
        row = self._row()
        drv, _ = make_driver([row], single_rows=[row])
        MOD._driver = drv
        result = run(MOD._neo4j_add_relationship("r1", "d1", "DEPENDS_ON"))
        assert result["from"] == "r1"
        assert result["to"] == "d1"
        assert result["type"] == "DEPENDS_ON"

    def test_session_no_leak(self):
        row = self._row()
        drv, tracker = make_driver([row], single_rows=[row])
        MOD._driver = drv
        run(MOD._neo4j_add_relationship("r1", "d1", "DEPENDS_ON"))
        assert tracker.count("open") == tracker.count("close")

    def test_uses_merge_cypher(self):
        row = self._row()
        drv, _ = make_driver([row], single_rows=[row])
        MOD._driver = drv
        run(MOD._neo4j_add_relationship("r1", "d1", "DEPENDS_ON"))
        assert any("MERGE" in q for q in drv.all_queries)

    def test_fallback_uuid_when_record_none(self):
        drv, _ = make_driver([], single_rows=[None])
        MOD._driver = drv
        result = run(MOD._neo4j_add_relationship("r1", "d1", "DEPENDS_ON"))
        assert "relationshipId" in result


# =============================================================================
# _neo4j_delete_node_and_rels
# =============================================================================

class TestNeo4jDeleteNode:

    def test_session_no_leak(self):
        drv, tracker = make_driver([])
        MOD._driver = drv
        run(MOD._neo4j_delete_node_and_rels("r1"))
        assert tracker.count("open") == tracker.count("close")

    def test_detach_delete_in_query(self):
        drv, _ = make_driver([])
        MOD._driver = drv
        run(MOD._neo4j_delete_node_and_rels("r1"))
        assert any("DETACH DELETE" in q for q in drv.all_queries)


# =============================================================================
# _neo4j_stats
# =============================================================================

class TestNeo4jStats:

    def test_session_no_leak(self):
        drv, tracker = make_driver(
            [{"cnt": 0}], [{"cnt": 0}], [], [],
            single_rows=[{"cnt": 0}, {"cnt": 0}, None, None],
        )
        MOD._driver = drv
        run(MOD._neo4j_stats())
        assert tracker.count("open") == tracker.count("close")

    def test_all_keys_present(self):
        drv, _ = make_driver(
            [{"cnt": 5}], [{"cnt": 10}],
            [{"label": "Repository", "cnt": 3}],
            [{"t": "DEPENDS_ON", "cnt": 7}],
            single_rows=[{"cnt": 5}, {"cnt": 10}, None, None],
        )
        MOD._driver = drv
        result = run(MOD._neo4j_stats())
        for key in ("totalNodes", "totalRelationships", "nodesByLabel", "relationshipsByType"):
            assert key in result

    def test_zero_values_on_empty_graph(self):
        drv, _ = make_driver(
            [], [], [], [],
            single_rows=[None, None, None, None],
        )
        MOD._driver = drv
        result = run(MOD._neo4j_stats())
        assert result["totalNodes"] == 0
        assert result["totalRelationships"] == 0


# =============================================================================
# _neo4j_dep_analysis — SESSION LEAK REGRESSION
# =============================================================================

class TestNeo4jDepAnalysis:
    """
    Primary regression suite for the session leak bug (e9963e1).
    A mismatch between open and close counts == new leak detected.
    """

    def _dep_rows(self):
        return [
            {"depId": "dep:pip:requests:2.31", "name": "requests",
             "version": "2.31", "ecosystem": "pip", "inDegree": 3},
            {"depId": "dep:pip:fastapi:0.110", "name": "fastapi",
             "version": "0.110", "ecosystem": "pip", "inDegree": 1},
        ]

    def test_no_session_leak_with_deps(self):
        """THE regression test: every open must have a matching close."""
        drv, tracker = make_driver(
            self._dep_rows(),   # 1. main dep query
            [{"cnt": 4}],       # 2. total repos count
            [],                  # 3. circular deps
            single_rows=[None, {"cnt": 4}, None],
        )
        MOD._driver = drv
        run(MOD._neo4j_dep_analysis("r1"))
        assert tracker.count("open") == tracker.count("close"), (
            f"SESSION LEAK: opens={tracker.count('open')} "
            f"closes={tracker.count('close')}"
        )

    def test_no_session_leak_empty_repo(self):
        """Early-return path must also close cleanly."""
        drv, tracker = make_driver([], single_rows=[None])
        MOD._driver = drv
        run(MOD._neo4j_dep_analysis("r1"))
        assert tracker.count("open") == tracker.count("close")

    def test_exactly_three_sessions_for_full_analysis(self):
        drv, tracker = make_driver(
            self._dep_rows(), [{"cnt": 5}], [],
            single_rows=[None, {"cnt": 5}, None],
        )
        MOD._driver = drv
        run(MOD._neo4j_dep_analysis("r1"))
        # 1 main, 1 repo count, 1 circular
        assert tracker.count("open") == 3
        assert tracker.count("close") == 3

    def test_returns_zero_on_empty(self):
        drv, _ = make_driver([], single_rows=[None])
        MOD._driver = drv
        result = run(MOD._neo4j_dep_analysis("r1"))
        assert result["totalDependencies"] == 0

    def test_total_deps_matches_row_count(self):
        drv, _ = make_driver(
            self._dep_rows(), [{"cnt": 5}], [],
            single_rows=[None, {"cnt": 5}, None],
        )
        MOD._driver = drv
        assert run(MOD._neo4j_dep_analysis("r1"))["totalDependencies"] == 2

    def test_critical_nodes_filtered_by_threshold(self):
        drv, _ = make_driver(
            self._dep_rows(), [{"cnt": 4}], [],
            single_rows=[None, {"cnt": 4}, None],
        )
        MOD._driver = drv
        result = run(MOD._neo4j_dep_analysis("r1"))
        names = [c["name"] for c in result["criticalNodes"]]
        assert "requests" in names   # inDegree=3 >= 2
        assert "fastapi" not in names  # inDegree=1 < 2

    def test_bottleneck_score_normalised(self):
        drv, _ = make_driver(
            self._dep_rows(), [{"cnt": 4}], [],
            single_rows=[None, {"cnt": 4}, None],
        )
        MOD._driver = drv
        score = run(MOD._neo4j_dep_analysis("r1"))["bottleneckScore"]
        assert 0.0 <= score <= 1.0

    def test_circular_deps_extracted(self):
        drv, _ = make_driver(
            self._dep_rows(), [{"cnt": 4}], [{"name": "evil-lib"}],
            single_rows=[None, {"cnt": 4}, None],
        )
        MOD._driver = drv
        result = run(MOD._neo4j_dep_analysis("r1"))
        assert "evil-lib" in result["circularDependencies"]

    def test_ecosystem_breakdown_aggregated(self):
        drv, _ = make_driver(
            self._dep_rows(), [{"cnt": 2}], [],
            single_rows=[None, {"cnt": 2}, None],
        )
        MOD._driver = drv
        result = run(MOD._neo4j_dep_analysis("r1"))
        assert result["ecosystemBreakdown"].get("pip", 0) == 2


# =============================================================================
# _neo4j_timeline — session lifecycle
# =============================================================================

class TestNeo4jTimeline:

    def _commit_rows(self):
        return [{"sha": "abc123", "message": "feat: auth",
                 "authorEmail": "dev@x.com", "authorName": "Dev",
                 "committedAt": _iso(1), "filesChanged": 3}]

    def _dep_rows(self):
        return [{"name": "requests", "ecosystem": "pip",
                 "version": "2.31", "createdAt": _iso(5)}]

    def test_no_leak_commit_only(self):
        drv, tracker = make_driver(self._commit_rows())
        MOD._driver = drv
        run(MOD._neo4j_timeline("r1", None, None, 50, "commit"))
        assert tracker.count("open") == tracker.count("close")

    def test_no_leak_dependency_only(self):
        drv, tracker = make_driver(self._dep_rows())
        MOD._driver = drv
        run(MOD._neo4j_timeline("r1", None, None, 50, "dependency"))
        assert tracker.count("open") == tracker.count("close")

    def test_no_leak_all_events(self):
        drv, tracker = make_driver(self._commit_rows(), self._dep_rows())
        MOD._driver = drv
        run(MOD._neo4j_timeline("r1", None, None, 50, "all"))
        assert tracker.count("open") == tracker.count("close")

    def test_commit_in_result(self):
        drv, _ = make_driver(self._commit_rows())
        MOD._driver = drv
        events = run(MOD._neo4j_timeline("r1", None, None, 50, "commit"))
        assert any(e["sha"] == "abc123" for e in events)

    def test_dependency_in_result(self):
        drv, _ = make_driver(self._dep_rows())
        MOD._driver = drv
        events = run(MOD._neo4j_timeline("r1", None, None, 50, "dependency"))
        assert any(e["dependencyName"] == "requests" for e in events)

    def test_since_param_in_query(self):
        drv, _ = make_driver(self._commit_rows())
        MOD._driver = drv
        run(MOD._neo4j_timeline("r1", _iso(7), None, 50, "commit"))
        assert any("$since" in q for q in drv.all_queries)

    def test_until_param_in_query(self):
        drv, _ = make_driver(self._commit_rows())
        MOD._driver = drv
        run(MOD._neo4j_timeline("r1", None, _iso(0), 50, "commit"))
        assert any("$until" in q for q in drv.all_queries)

    def test_no_filters_in_query_when_not_provided(self):
        drv, _ = make_driver(self._commit_rows())
        MOD._driver = drv
        run(MOD._neo4j_timeline("r1", None, None, 50, "commit"))
        commit_q = [q for q in drv.all_queries if "Commit" in q]
        assert not any("$since" in q for q in commit_q)

    def test_empty_result_on_no_data(self):
        drv, _ = make_driver([], [])
        MOD._driver = drv
        assert run(MOD._neo4j_timeline("r1", None, None, 50, "all")) == []

    def test_events_descending_order(self):
        rows = [
            {"sha": "new", "message": "new", "authorEmail": "a@b.com",
             "authorName": "A", "committedAt": _iso(1), "filesChanged": 1},
            {"sha": "old", "message": "old", "authorEmail": "a@b.com",
             "authorName": "A", "committedAt": _iso(10), "filesChanged": 1},
        ]
        drv, _ = make_driver(rows)
        MOD._driver = drv
        events = run(MOD._neo4j_timeline("r1", None, None, 50, "commit"))
        timestamps = [e["timestamp"] for e in events]
        assert timestamps == sorted(timestamps, reverse=True)


# =============================================================================
# _neo4j_list_relationships
# =============================================================================

class TestNeo4jListRelationships:

    def _row(self):
        return {"r": {"relationshipId": "rel-1", "createdAt": _iso()},
                "fromId": "r1", "toId": "d1", "relType": "DEPENDS_ON"}

    def test_empty_returns_empty_list(self):
        drv, _ = make_driver([])
        MOD._driver = drv
        assert run(MOD._neo4j_list_relationships(None, None, None)) == []

    def test_session_no_leak(self):
        drv, tracker = make_driver([self._row()])
        MOD._driver = drv
        run(MOD._neo4j_list_relationships(None, None, None))
        assert tracker.count("open") == tracker.count("close")

    def test_rel_type_backtick_quoted(self):
        drv, _ = make_driver([])
        MOD._driver = drv
        run(MOD._neo4j_list_relationships(None, None, "DEPENDS_ON"))
        assert any("`DEPENDS_ON`" in q for q in drv.all_queries)

    def test_unknown_rel_type_raises(self):
        drv, _ = make_driver([])
        MOD._driver = drv
        with pytest.raises(ValueError, match="Unknown relationship type"):
            run(MOD._neo4j_list_relationships(None, None, "EVIL_REL"))

    def test_result_fields_correct(self):
        drv, _ = make_driver([self._row()])
        MOD._driver = drv
        result = run(MOD._neo4j_list_relationships(None, None, None))
        assert result[0]["from"] == "r1"
        assert result[0]["to"] == "d1"
        assert result[0]["type"] == "DEPENDS_ON"
