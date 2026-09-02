"""
Tests for pattern_detector.py — architectural pattern classification.

Covers:
- Microservices detection (multiple services + Kafka topics/rels)
- Layered detection (controller/service/repository/model naming)
- Hexagonal detection (ports/adapters/domain naming)
- Modular Monolith detection (single service + multiple modules)
- Unknown pattern (insufficient data)
- Confidence score ranges
- Evidence population
"""
import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "graph-service"))

from analyzers.pattern_detector import detect_patterns, PatternResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _node(label: str, name: str) -> dict:
    return {"label": label, "properties": {"name": name}}


def _rel(src: str, tgt: str, rel_type: str) -> dict:
    return {"sourceId": src, "targetId": tgt, "type": rel_type}


def _make_graph(nodes: dict, rels: list) -> dict:
    return {"nodes": nodes, "relationships": rels}


# ---------------------------------------------------------------------------
# Microservices
# ---------------------------------------------------------------------------

class TestMicroservicesPattern:
    def _microservices_graph(self, n_services: int = 3, n_kafka: int = 2) -> dict:
        nodes = {}
        for i in range(n_services):
            nodes[f"svc-{i}"] = _node("Service", f"service-{i}")
        for i in range(n_kafka):
            nodes[f"topic-{i}"] = _node("KafkaTopic", f"topic.{i}")
        rels = []
        for i in range(n_services):
            rels.append(_rel(f"svc-{i}", f"topic-{i % n_kafka}", "PRODUCES"))
            rels.append(_rel(f"svc-{(i+1) % n_services}", f"topic-{i % n_kafka}", "CONSUMES"))
        return _make_graph(nodes, rels)

    def test_detects_microservices_with_3_services(self):
        graph = self._microservices_graph(3, 2)
        result = detect_patterns(graph)
        assert result.pattern == "Microservices"

    def test_confidence_above_threshold_for_microservices(self):
        graph = self._microservices_graph(4, 3)
        result = detect_patterns(graph)
        assert result.confidence >= 0.6

    def test_evidence_mentions_services(self):
        graph = self._microservices_graph(3, 2)
        result = detect_patterns(graph)
        rules = [e.rule for e in result.evidence]
        assert any("service" in r or "kafka" in r.lower() for r in rules)

    def test_two_services_without_kafka_not_microservices(self):
        nodes = {
            "s1": _node("Service", "alpha"),
            "s2": _node("Service", "beta"),
        }
        result = detect_patterns(_make_graph(nodes, []))
        # With only 2 services and no Kafka, confidence should be low
        assert result.pattern != "Microservices" or result.confidence < 0.5

    def test_single_service_not_microservices(self):
        nodes = {"s1": _node("Service", "monolith")}
        result = detect_patterns(_make_graph(nodes, []))
        assert result.pattern != "Microservices"


# ---------------------------------------------------------------------------
# Layered architecture
# ---------------------------------------------------------------------------

class TestLayeredPattern:
    def _layered_graph(self) -> dict:
        nodes = {
            "c1": _node("Class", "UserController"),
            "c2": _node("Class", "UserService"),
            "c3": _node("Class", "UserRepository"),
            "c4": _node("Class", "UserModel"),
        }
        rels = [
            _rel("c1", "c2", "DEPENDS_ON"),
            _rel("c2", "c3", "DEPENDS_ON"),
            _rel("c3", "c4", "DEPENDS_ON"),
        ]
        return _make_graph(nodes, rels)

    def test_detects_layered_pattern(self):
        result = detect_patterns(self._layered_graph())
        assert result.pattern == "Layered"

    def test_layered_confidence_above_threshold(self):
        result = detect_patterns(self._layered_graph())
        assert result.confidence >= 0.7

    def test_layered_evidence_has_all_layers(self):
        result = detect_patterns(self._layered_graph())
        rules = [e.rule for e in result.evidence]
        assert any("controller" in r for r in rules)
        assert any("service" in r for r in rules)
        assert any("repository" in r for r in rules)
        assert any("model" in r for r in rules)

    def test_partial_layers_lower_confidence(self):
        """Only controller + service → partial layered."""
        nodes = {
            "c1": _node("Class", "UserController"),
            "c2": _node("Class", "UserService"),
        }
        result = detect_patterns(_make_graph(nodes, []))
        if result.pattern == "Layered":
            assert result.confidence < 0.75

    def test_alternative_layer_names_detected(self):
        """handler, dao, entity should trigger layer detection."""
        nodes = {
            "n1": _node("Class", "RequestHandler"),
            "n2": _node("Class", "ProductUsecase"),
            "n3": _node("Class", "OrderDAO"),
            "n4": _node("Class", "ProductEntity"),
        }
        result = detect_patterns(_make_graph(nodes, []))
        assert result.pattern == "Layered"


# ---------------------------------------------------------------------------
# Hexagonal architecture
# ---------------------------------------------------------------------------

class TestHexagonalPattern:
    def _hexagonal_graph(self) -> dict:
        nodes = {
            "n1": _node("Module", "ports"),
            "n2": _node("Module", "adapters"),
            "n3": _node("Module", "domain"),
        }
        return _make_graph(nodes, [])

    def test_detects_hexagonal_pattern(self):
        result = detect_patterns(self._hexagonal_graph())
        assert result.pattern == "Hexagonal"

    def test_hexagonal_confidence(self):
        result = detect_patterns(self._hexagonal_graph())
        assert result.confidence >= 0.8

    def test_partial_hexagonal(self):
        """Only ports + adapters without domain → still hexagonal-leaning."""
        nodes = {
            "n1": _node("Module", "ports"),
            "n2": _node("Module", "adapters"),
        }
        result = detect_patterns(_make_graph(nodes, []))
        if result.pattern == "Hexagonal":
            assert result.confidence < 0.75  # partial

    def test_infrastructure_counts_as_adapter(self):
        nodes = {
            "n1": _node("Module", "ports"),
            "n2": _node("Module", "infrastructure"),
            "n3": _node("Module", "domain"),
        }
        result = detect_patterns(_make_graph(nodes, []))
        assert result.pattern == "Hexagonal"


# ---------------------------------------------------------------------------
# Modular Monolith
# ---------------------------------------------------------------------------

class TestModularMonolithPattern:
    def _modular_graph(self) -> dict:
        nodes = {
            "svc": _node("Service", "MyApp"),
            "m1":  _node("Module", "billing"),
            "m2":  _node("Module", "shipping"),
            "m3":  _node("Module", "inventory"),
        }
        rels = [
            _rel("m1", "svc", "BELONGS_TO"),
            _rel("m2", "svc", "BELONGS_TO"),
            _rel("m3", "svc", "BELONGS_TO"),
        ]
        return _make_graph(nodes, rels)

    def test_detects_modular_monolith(self):
        result = detect_patterns(self._modular_graph())
        assert result.pattern == "ModularMonolith"

    def test_modular_monolith_confidence(self):
        result = detect_patterns(self._modular_graph())
        assert result.confidence >= 0.7

    def test_single_module_lower_confidence(self):
        nodes = {
            "svc": _node("Service", "MyApp"),
            "m1":  _node("Module", "billing"),
        }
        result = detect_patterns(_make_graph(nodes, []))
        if result.pattern == "ModularMonolith":
            assert result.confidence < 0.6


# ---------------------------------------------------------------------------
# Unknown / empty graph
# ---------------------------------------------------------------------------

class TestUnknownPattern:
    def test_empty_graph_returns_unknown(self):
        result = detect_patterns({"nodes": {}, "relationships": []})
        assert result.pattern == "Unknown"

    def test_single_repo_node_returns_unknown(self):
        nodes = {"r1": _node("Repository", "alpha-project")}
        result = detect_patterns(_make_graph(nodes, []))
        # A single Repository node with no services/modules → Unknown or very low confidence
        assert result.pattern == "Unknown" or result.confidence < 0.3

    def test_unknown_confidence_low(self):
        result = detect_patterns({"nodes": {}, "relationships": []})
        assert result.confidence < 0.2

    def test_result_has_description(self):
        result = detect_patterns({"nodes": {}, "relationships": []})
        assert isinstance(result.description, str)
        assert len(result.description) > 0


# ---------------------------------------------------------------------------
# to_dict serialization
# ---------------------------------------------------------------------------

class TestPatternResultSerialization:
    def test_to_dict_has_required_keys(self):
        result = detect_patterns({"nodes": {}, "relationships": []})
        d = result.to_dict()
        assert "pattern" in d
        assert "confidence" in d
        assert "evidence" in d
        assert "description" in d

    def test_confidence_rounded(self):
        nodes = {f"svc-{i}": _node("Service", f"s{i}") for i in range(3)}
        nodes.update({f"t-{i}": _node("KafkaTopic", f"t{i}") for i in range(2)})
        rels = [{"sourceId": "svc-0", "targetId": "t-0", "type": "PRODUCES"},
                {"sourceId": "svc-1", "targetId": "t-0", "type": "CONSUMES"},
                {"sourceId": "svc-0", "targetId": "t-1", "type": "PRODUCES"},
                {"sourceId": "svc-2", "targetId": "t-1", "type": "CONSUMES"}]
        result = detect_patterns(_make_graph(nodes, rels))
        d = result.to_dict()
        # Confidence should be rounded to 3 decimal places
        assert d["confidence"] == round(d["confidence"], 3)
