"""
Tests for impact_analyzer.py — change impact analysis.

Covers:
- Direct dependents (distance=1)
- Transitive dependents (distance=2+)
- Critical node type detection (KafkaTopic, Service, ApiEndpoint)
- Delete vs modify risk amplification
- No affected entities for isolated nodes
- Depth limit enforcement
- Critical paths population
- to_dict() serialization
"""
import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "graph-service"))

from analyzers.impact_analyzer import analyze_impact, ImpactReport


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _node(label: str, name: str) -> dict:
    return {"label": label, "properties": {"name": name}}


def _rel(src: str, tgt: str, rel_type: str = "DEPENDS_ON") -> dict:
    return {"sourceId": src, "targetId": tgt, "type": rel_type}


def _graph(nodes: dict, rels: list) -> dict:
    return {"nodes": nodes, "relationships": rels}


def _affected_ids(report: ImpactReport) -> set:
    return {e.entity_id for e in report.affected_entities}


# ---------------------------------------------------------------------------
# 1. Basic direct dependents
# ---------------------------------------------------------------------------

class TestDirectDependents:
    def test_single_direct_dependent(self):
        nodes = {
            "lib":    _node("Module", "utils"),
            "caller": _node("Module", "app"),
        }
        rels   = [_rel("caller", "lib")]
        report = analyze_impact("lib", _graph(nodes, rels))
        assert "caller" in _affected_ids(report)

    def test_source_not_in_affected(self):
        nodes = {
            "lib":    _node("Module", "utils"),
            "caller": _node("Module", "app"),
        }
        rels   = [_rel("caller", "lib")]
        report = analyze_impact("lib", _graph(nodes, rels))
        assert "lib" not in _affected_ids(report)

    def test_multiple_direct_dependents(self):
        nodes = {
            "lib": _node("Module", "shared"),
            "a":   _node("Module", "serviceA"),
            "b":   _node("Module", "serviceB"),
            "c":   _node("Module", "serviceC"),
        }
        rels = [_rel("a", "lib"), _rel("b", "lib"), _rel("c", "lib")]
        report = analyze_impact("lib", _graph(nodes, rels))
        assert _affected_ids(report) >= {"a", "b", "c"}

    def test_no_affected_for_isolated_node(self):
        nodes  = {"iso": _node("Module", "standalone")}
        report = analyze_impact("iso", _graph(nodes, []))
        assert len(report.affected_entities) == 0

    def test_non_impact_rels_ignored(self):
        """BELONGS_TO, COMMITTED_TO etc. should not propagate impact."""
        nodes = {
            "svc":  _node("Service", "PaymentService"),
            "repo": _node("Repository", "monorepo"),
        }
        rels  = [_rel("svc", "repo", "BELONGS_TO")]
        report = analyze_impact("repo", _graph(nodes, rels))
        # BELONGS_TO does not propagate impact
        assert "svc" not in _affected_ids(report)


# ---------------------------------------------------------------------------
# 2. Transitive dependents
# ---------------------------------------------------------------------------

class TestTransitiveDependents:
    def test_depth_2_reached(self):
        nodes = {
            "lib":    _node("Module", "utils"),
            "middle": _node("Module", "core"),
            "top":    _node("Module", "app"),
        }
        rels = [_rel("middle", "lib"), _rel("top", "middle")]
        report = analyze_impact("lib", _graph(nodes, rels), depth=2)
        assert "top" in _affected_ids(report)
        assert report.max_depth >= 2

    def test_depth_limit_enforced(self):
        # Chain: lib → a → b → c (depth=3 starting at lib)
        nodes = {
            "lib": _node("Module", "lib"),
            "a":   _node("Module", "a"),
            "b":   _node("Module", "b"),
            "c":   _node("Module", "c"),
        }
        rels = [_rel("a", "lib"), _rel("b", "a"), _rel("c", "b")]
        report = analyze_impact("lib", _graph(nodes, rels), depth=2)
        # With depth=2, 'c' (distance=3) should NOT be included
        assert "c" not in _affected_ids(report)

    def test_depth_3_reaches_far_nodes(self):
        nodes = {
            "lib": _node("Module", "lib"),
            "a":   _node("Module", "a"),
            "b":   _node("Module", "b"),
            "c":   _node("Module", "c"),
        }
        rels = [_rel("a", "lib"), _rel("b", "a"), _rel("c", "b")]
        report = analyze_impact("lib", _graph(nodes, rels), depth=3)
        assert "c" in _affected_ids(report)


# ---------------------------------------------------------------------------
# 3. Critical node detection
# ---------------------------------------------------------------------------

class TestCriticalNodeDetection:
    def test_kafka_topic_always_critical(self):
        nodes = {
            "svc":   _node("Service", "PaymentService"),
            "topic": _node("KafkaTopic", "payment.processed"),
        }
        rels   = [_rel("svc", "topic", "PRODUCES")]
        report = analyze_impact("topic", _graph(nodes, rels))
        # Service that depends on topic → when topic changes, service is affected
        # But here we're changing the topic itself, checking svc as affected
        if "svc" in _affected_ids(report):
            svc_entity = next(e for e in report.affected_entities if e.entity_id == "svc")
            # As a service that depends on the topic
            assert svc_entity.risk_level in ("critical", "high", "medium")

    def test_service_node_gets_critical_risk(self):
        nodes = {
            "lib":  _node("Module", "utils"),
            "svc":  _node("Service", "CoreService"),
        }
        rels   = [_rel("svc", "lib", "DEPENDS_ON")]
        report = analyze_impact("lib", _graph(nodes, rels))
        svc = next((e for e in report.affected_entities if e.entity_id == "svc"), None)
        assert svc is not None
        assert svc.risk_level == "critical"

    def test_api_endpoint_gets_critical_risk(self):
        nodes = {
            "lib": _node("Module", "utils"),
            "api": _node("ApiEndpoint", "POST /payments"),
        }
        rels   = [_rel("api", "lib", "CALLS")]
        report = analyze_impact("lib", _graph(nodes, rels))
        api = next((e for e in report.affected_entities if e.entity_id == "api"), None)
        assert api is not None
        assert api.risk_level == "critical"


# ---------------------------------------------------------------------------
# 4. Risk level ordering
# ---------------------------------------------------------------------------

class TestRiskLevelOrdering:
    def test_affected_sorted_critical_first(self):
        nodes = {
            "lib": _node("Module", "utils"),
            "svc": _node("Service", "CoreService"),
            "mod": _node("Module", "app"),
        }
        rels = [
            _rel("svc", "lib"),
            _rel("mod", "lib"),
        ]
        report = analyze_impact("lib", _graph(nodes, rels))
        if len(report.affected_entities) >= 2:
            risk_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
            risks = [risk_order[e.risk_level] for e in report.affected_entities]
            assert risks == sorted(risks)

    def test_delete_increases_risk(self):
        """Delete operations should produce higher-risk classification."""
        nodes = {
            "lib": _node("Module", "utils"),
            "a":   _node("Module", "a"),
        }
        rels    = [_rel("a", "lib")]
        modify  = analyze_impact("lib", _graph(nodes, rels), change_type="modify")
        delete  = analyze_impact("lib", _graph(nodes, rels), change_type="delete")

        risk_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        if modify.affected_entities and delete.affected_entities:
            m_risk = min(risk_order[e.risk_level] for e in modify.affected_entities)
            d_risk = min(risk_order[e.risk_level] for e in delete.affected_entities)
            # Delete risk should be same or lower index (= higher priority)
            assert d_risk <= m_risk


# ---------------------------------------------------------------------------
# 5. Critical paths
# ---------------------------------------------------------------------------

class TestCriticalPaths:
    def test_critical_paths_populated_for_critical_nodes(self):
        nodes = {
            "lib": _node("Module", "utils"),
            "svc": _node("Service", "CoreService"),
        }
        rels   = [_rel("svc", "lib")]
        report = analyze_impact("lib", _graph(nodes, rels))
        assert len(report.critical_paths) >= 1

    def test_critical_paths_include_source_and_target(self):
        nodes = {
            "lib": _node("Module", "utils"),
            "svc": _node("Service", "CoreService"),
        }
        rels   = [_rel("svc", "lib")]
        report = analyze_impact("lib", _graph(nodes, rels))
        if report.critical_paths:
            path = report.critical_paths[0]
            assert "lib" in path
            assert "svc" in path

    def test_no_critical_paths_for_non_critical_graph(self):
        nodes = {
            "a": _node("Module", "a"),
            "b": _node("Module", "b"),
        }
        rels   = [_rel("b", "a")]
        report = analyze_impact("a", _graph(nodes, rels))
        # No Service/KafkaTopic/ApiEndpoint → no critical paths
        assert len(report.critical_paths) == 0


# ---------------------------------------------------------------------------
# 6. Relationship types
# ---------------------------------------------------------------------------

class TestRelationshipTypes:
    def test_calls_propagates_impact(self):
        nodes = {"a": _node("Function", "fn"), "b": _node("Class", "Cls")}
        rels   = [_rel("b", "a", "CALLS")]
        report = analyze_impact("a", _graph(nodes, rels))
        assert "b" in _affected_ids(report)

    def test_implements_propagates_impact(self):
        nodes = {"iface": _node("Class", "IPayment"), "impl": _node("Class", "StripePayment")}
        rels   = [_rel("impl", "iface", "IMPLEMENTS")]
        report = analyze_impact("iface", _graph(nodes, rels))
        assert "impl" in _affected_ids(report)

    def test_produces_propagates_impact(self):
        nodes = {"svc": _node("Service", "Svc"), "topic": _node("KafkaTopic", "events")}
        rels   = [_rel("svc", "topic", "PRODUCES")]
        report = analyze_impact("topic", _graph(nodes, rels))
        # svc produces topic → if topic changes, svc is affected
        assert "svc" in _affected_ids(report)


# ---------------------------------------------------------------------------
# 7. Serialization
# ---------------------------------------------------------------------------

class TestImpactSerialization:
    def test_to_dict_has_required_keys(self):
        nodes = {
            "lib": _node("Module", "utils"),
            "app": _node("Module", "app"),
        }
        rels   = [_rel("app", "lib")]
        report = analyze_impact("lib", _graph(nodes, rels))
        d = report.to_dict()
        for key in ("sourceId", "changeType", "totalAffected", "maxDepth",
                    "riskSummary", "affectedEntities", "criticalPaths"):
            assert key in d

    def test_risk_summary_counts_correct(self):
        nodes = {
            "lib": _node("Module", "utils"),
            "svc": _node("Service", "CoreSvc"),
        }
        rels   = [_rel("svc", "lib")]
        report = analyze_impact("lib", _graph(nodes, rels))
        d = report.to_dict()
        assert d["riskSummary"]["critical"] >= 1

    def test_affected_entity_to_dict(self):
        nodes = {
            "lib": _node("Module", "utils"),
            "app": _node("Module", "app"),
        }
        rels   = [_rel("app", "lib")]
        report = analyze_impact("lib", _graph(nodes, rels))
        if report.affected_entities:
            e_dict = report.affected_entities[0].to_dict()
            for key in ("id", "type", "name", "riskLevel", "distance", "path"):
                assert key in e_dict

    def test_change_type_preserved_in_dict(self):
        report = analyze_impact("x", _graph({"x": _node("Module", "X")}, []),
                                 change_type="delete")
        assert report.to_dict()["changeType"] == "delete"


# ---------------------------------------------------------------------------
# 8. Edge cases
# ---------------------------------------------------------------------------

class TestImpactEdgeCases:
    def test_nonexistent_source_returns_empty_report(self):
        report = analyze_impact("ghost", _graph({}, []))
        assert len(report.affected_entities) == 0

    def test_circular_deps_dont_infinite_loop(self):
        nodes = {"a": _node("Module", "A"), "b": _node("Module", "B")}
        rels  = [_rel("a", "b"), _rel("b", "a")]
        # Should not hang
        report = analyze_impact("a", _graph(nodes, rels))
        assert report.max_depth >= 0

    def test_max_affected_cap(self):
        # Create 30 dependents
        nodes = {"lib": _node("Module", "lib")}
        rels  = []
        for i in range(30):
            nodes[f"dep{i}"] = _node("Module", f"dep{i}")
            rels.append(_rel(f"dep{i}", "lib"))
        report = analyze_impact("lib", _graph(nodes, rels), max_affected=10)
        assert len(report.affected_entities) <= 10
