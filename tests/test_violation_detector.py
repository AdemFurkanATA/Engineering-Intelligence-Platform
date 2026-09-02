"""
Tests for violation_detector.py — architectural violation detection.

Covers:
- God class detection (high out-degree / method count)
- Dependency cycle detection (DFS)
- Orphan node detection
- Oversized service detection
- Layer violation detection (inner → outer dependency)
- Dead code candidate detection
- ViolationReport.to_dict() serialization
- Empty graph produces no violations
"""
import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "graph-service"))

from analyzers.violation_detector import detect_violations, Violation, ViolationReport


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _node(label: str, name: str, extra: dict = None) -> dict:
    props = {"name": name}
    if extra:
        props.update(extra)
    return {"label": label, "properties": props}


def _rel(src: str, tgt: str, rel_type: str) -> dict:
    return {"sourceId": src, "targetId": tgt, "type": rel_type}


def _graph(nodes: dict, rels: list) -> dict:
    return {"nodes": nodes, "relationships": rels}


def _violation_types(report: ViolationReport) -> list:
    return [v.violation_type for v in report.violations]


# ---------------------------------------------------------------------------
# God class
# ---------------------------------------------------------------------------

class TestGodClassDetection:
    def test_class_above_threshold_flagged(self):
        nodes = {
            "cls-big": _node("Class", "GodClass", {"methodCount": 25}),
        }
        report = detect_violations(_graph(nodes, []), god_class_threshold=20)
        assert "god_class" in _violation_types(report)

    def test_class_at_threshold_not_flagged(self):
        nodes = {
            "cls-ok": _node("Class", "OkClass", {"methodCount": 20}),
        }
        report = detect_violations(_graph(nodes, []), god_class_threshold=20)
        assert "god_class" not in _violation_types(report)

    def test_class_below_threshold_not_flagged(self):
        nodes = {
            "cls-small": _node("Class", "SmallClass", {"methodCount": 5}),
        }
        report = detect_violations(_graph(nodes, []), god_class_threshold=20)
        assert "god_class" not in _violation_types(report)

    def test_out_degree_used_when_no_method_count(self):
        """If methodCount is absent, out-degree (relationship count) is used."""
        nodes = {"cls": _node("Class", "BigClass")}
        rels  = [_rel("cls", f"fn-{i}", "CALLS") for i in range(25)]
        for i in range(25):
            nodes[f"fn-{i}"] = _node("Function", f"fn{i}")
        report = detect_violations(_graph(nodes, rels), god_class_threshold=20)
        assert "god_class" in _violation_types(report)

    def test_god_class_severity_is_high(self):
        nodes = {"cls": _node("Class", "GodClass", {"methodCount": 30})}
        report = detect_violations(_graph(nodes, []), god_class_threshold=20)
        god_violations = [v for v in report.violations if v.violation_type == "god_class"]
        assert all(v.severity == "high" for v in god_violations)

    def test_non_class_nodes_not_flagged(self):
        """Function and Service nodes should not trigger god_class."""
        nodes = {
            "fn": _node("Function", "BigFunction", {"methodCount": 50}),
            "svc": _node("Service", "BigService", {"methodCount": 50}),
        }
        report = detect_violations(_graph(nodes, []), god_class_threshold=20)
        assert "god_class" not in _violation_types(report)


# ---------------------------------------------------------------------------
# Dependency cycles
# ---------------------------------------------------------------------------

class TestCycleDetection:
    def test_simple_two_node_cycle(self):
        nodes = {
            "a": _node("Module", "ModuleA"),
            "b": _node("Module", "ModuleB"),
        }
        rels = [_rel("a", "b", "DEPENDS_ON"), _rel("b", "a", "DEPENDS_ON")]
        report = detect_violations(_graph(nodes, rels))
        assert "dependency_cycle" in _violation_types(report)

    def test_three_node_cycle(self):
        nodes = {
            "a": _node("Class", "A"),
            "b": _node("Class", "B"),
            "c": _node("Class", "C"),
        }
        rels = [
            _rel("a", "b", "DEPENDS_ON"),
            _rel("b", "c", "DEPENDS_ON"),
            _rel("c", "a", "DEPENDS_ON"),
        ]
        report = detect_violations(_graph(nodes, rels))
        assert "dependency_cycle" in _violation_types(report)

    def test_no_cycle_detected_in_dag(self):
        nodes = {
            "a": _node("Class", "A"),
            "b": _node("Class", "B"),
            "c": _node("Class", "C"),
        }
        rels = [_rel("a", "b", "DEPENDS_ON"), _rel("b", "c", "DEPENDS_ON")]
        report = detect_violations(_graph(nodes, rels))
        assert "dependency_cycle" not in _violation_types(report)

    def test_cycle_violation_has_related_entities(self):
        nodes = {
            "a": _node("Class", "A"),
            "b": _node("Class", "B"),
        }
        rels = [_rel("a", "b", "DEPENDS_ON"), _rel("b", "a", "DEPENDS_ON")]
        report = detect_violations(_graph(nodes, rels))
        cycle_violations = [v for v in report.violations
                            if v.violation_type == "dependency_cycle"]
        assert any(len(v.related) > 0 for v in cycle_violations)

    def test_calls_rels_also_detect_cycles(self):
        nodes = {"a": _node("Function", "funcA"), "b": _node("Function", "funcB")}
        rels  = [_rel("a", "b", "CALLS"), _rel("b", "a", "CALLS")]
        report = detect_violations(_graph(nodes, rels))
        assert "dependency_cycle" in _violation_types(report)


# ---------------------------------------------------------------------------
# Orphan nodes
# ---------------------------------------------------------------------------

class TestOrphanDetection:
    def test_isolated_node_flagged(self):
        nodes = {
            "r1": {"label": "Repository", "properties": {"name": "repo"}},
            "m1": _node("Module", "OrphanModule"),
        }
        report = detect_violations(_graph(nodes, []))
        assert "orphan_node" in _violation_types(report)

    def test_connected_node_not_flagged(self):
        nodes = {
            "a": _node("Module", "A"),
            "b": _node("Module", "B"),
        }
        rels = [_rel("a", "b", "DEPENDS_ON")]
        report = detect_violations(_graph(nodes, rels))
        orphans = [v for v in report.violations if v.violation_type == "orphan_node"]
        assert all(v.entity_id not in ("a", "b") for v in orphans)

    def test_repository_node_not_flagged_as_orphan(self):
        """Repository nodes are expected to start with few relationships."""
        nodes = {"repo-1": {"label": "Repository", "properties": {"name": "repo"}}}
        report = detect_violations(_graph(nodes, []))
        assert "orphan_node" not in _violation_types(report)

    def test_orphan_severity_is_low(self):
        nodes = {"m1": _node("Module", "Orphan")}
        report = detect_violations(_graph(nodes, []))
        orphan_violations = [v for v in report.violations
                             if v.violation_type == "orphan_node"]
        assert all(v.severity == "low" for v in orphan_violations)


# ---------------------------------------------------------------------------
# Oversized service
# ---------------------------------------------------------------------------

class TestOversizedServiceDetection:
    def test_service_with_too_many_children_flagged(self):
        nodes = {"svc": _node("Service", "BigService")}
        for i in range(60):
            nodes[f"fn-{i}"] = _node("Function", f"fn{i}")
        rels = [_rel(f"fn-{i}", "svc", "BELONGS_TO") for i in range(60)]
        report = detect_violations(_graph(nodes, rels), service_size_threshold=50)
        assert "oversized_service" in _violation_types(report)

    def test_service_below_threshold_not_flagged(self):
        nodes = {"svc": _node("Service", "SmallService")}
        for i in range(20):
            nodes[f"fn-{i}"] = _node("Function", f"fn{i}")
        rels = [_rel(f"fn-{i}", "svc", "BELONGS_TO") for i in range(20)]
        report = detect_violations(_graph(nodes, rels), service_size_threshold=50)
        assert "oversized_service" not in _violation_types(report)

    def test_oversized_severity_is_medium(self):
        nodes = {"svc": _node("Service", "Svc")}
        for i in range(55):
            nodes[f"fn-{i}"] = _node("Function", f"fn{i}")
        rels = [_rel(f"fn-{i}", "svc", "BELONGS_TO") for i in range(55)]
        report = detect_violations(_graph(nodes, rels), service_size_threshold=50)
        svc_v = [v for v in report.violations if v.violation_type == "oversized_service"]
        assert all(v.severity == "medium" for v in svc_v)


# ---------------------------------------------------------------------------
# Layer violations
# ---------------------------------------------------------------------------

class TestLayerViolationDetection:
    def test_model_depending_on_controller_flagged(self):
        """Inner layer (model) should not depend on outer (controller)."""
        nodes = {
            "m": _node("Class", "UserModel"),
            "c": _node("Class", "UserController"),
        }
        rels = [_rel("m", "c", "DEPENDS_ON")]
        report = detect_violations(_graph(nodes, rels))
        assert "layer_violation" in _violation_types(report)

    def test_repository_depending_on_controller_flagged(self):
        nodes = {
            "r": _node("Class", "UserRepository"),
            "c": _node("Class", "UserController"),
        }
        rels = [_rel("r", "c", "DEPENDS_ON")]
        report = detect_violations(_graph(nodes, rels))
        assert "layer_violation" in _violation_types(report)

    def test_controller_depending_on_service_allowed(self):
        nodes = {
            "ctrl": _node("Class", "UserController"),
            "svc":  _node("Class", "UserService"),
        }
        rels = [_rel("ctrl", "svc", "DEPENDS_ON")]
        report = detect_violations(_graph(nodes, rels))
        layer_v = [v for v in report.violations if v.violation_type == "layer_violation"]
        assert len(layer_v) == 0

    def test_service_depending_on_repository_allowed(self):
        nodes = {
            "svc":  _node("Class", "OrderService"),
            "repo": _node("Class", "OrderRepository"),
        }
        rels = [_rel("svc", "repo", "DEPENDS_ON")]
        report = detect_violations(_graph(nodes, rels))
        layer_v = [v for v in report.violations if v.violation_type == "layer_violation"]
        assert len(layer_v) == 0

    def test_layer_violation_severity_medium(self):
        nodes = {
            "m": _node("Class", "UserModel"),
            "c": _node("Class", "UserController"),
        }
        rels = [_rel("m", "c", "CALLS")]
        report = detect_violations(_graph(nodes, rels))
        layer_v = [v for v in report.violations if v.violation_type == "layer_violation"]
        assert all(v.severity == "medium" for v in layer_v)


# ---------------------------------------------------------------------------
# Dead code candidates
# ---------------------------------------------------------------------------

class TestDeadCodeDetection:
    def test_uncalled_function_flagged(self):
        nodes = {
            "fn-used":   _node("Function", "usedFunction"),
            "fn-unused": _node("Function", "unusedFunction"),
            "caller":    _node("Class", "CallerClass"),
        }
        rels = [_rel("caller", "fn-used", "CALLS")]
        report = detect_violations(_graph(nodes, rels))
        dead = [v for v in report.violations if v.violation_type == "dead_code_candidate"]
        dead_ids = [v.entity_id for v in dead]
        assert "fn-unused" in dead_ids
        assert "fn-used" not in dead_ids

    def test_called_function_not_flagged(self):
        nodes = {
            "fn":     _node("Function", "utilFunction"),
            "caller": _node("Class", "MyClass"),
        }
        rels = [_rel("caller", "fn", "CALLS")]
        report = detect_violations(_graph(nodes, rels))
        dead = [v for v in report.violations if v.violation_type == "dead_code_candidate"]
        assert all(v.entity_id != "fn" for v in dead)

    def test_init_methods_excluded(self):
        nodes = {"fn": _node("Function", "__init__")}
        report = detect_violations(_graph(nodes, []))
        dead = [v for v in report.violations if v.violation_type == "dead_code_candidate"]
        assert len(dead) == 0

    def test_test_functions_excluded(self):
        nodes = {"fn": _node("Function", "test_my_function")}
        report = detect_violations(_graph(nodes, []))
        dead = [v for v in report.violations if v.violation_type == "dead_code_candidate"]
        assert len(dead) == 0

    def test_dead_code_severity_is_low(self):
        nodes = {"fn": _node("Function", "orphanFn")}
        report = detect_violations(_graph(nodes, []))
        dead = [v for v in report.violations if v.violation_type == "dead_code_candidate"]
        assert all(v.severity == "low" for v in dead)


# ---------------------------------------------------------------------------
# Empty graph / edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_empty_graph_no_violations(self):
        report = detect_violations({"nodes": {}, "relationships": []})
        assert len(report.violations) == 0

    def test_to_dict_has_required_keys(self):
        report = detect_violations({"nodes": {}, "relationships": []})
        d = report.to_dict()
        assert "violations" in d
        assert "totalCount" in d
        assert "bySeverity" in d

    def test_by_severity_counts_correct(self):
        nodes = {
            "cls": _node("Class", "GodClass", {"methodCount": 25}),
        }
        report = detect_violations(_graph(nodes, []), god_class_threshold=20)
        d = report.to_dict()
        assert d["bySeverity"]["high"] >= 1

    def test_violation_to_dict_structure(self):
        nodes = {"cls": _node("Class", "GodClass", {"methodCount": 25})}
        report = detect_violations(_graph(nodes, []), god_class_threshold=20)
        v_dict = report.violations[0].to_dict()
        assert "type" in v_dict
        assert "severity" in v_dict
        assert "entityId" in v_dict
        assert "description" in v_dict
        assert "recommendation" in v_dict
