"""
tests/test_search_engine.py

Unit tests for the Engineering Intelligence Platform — search-service.

Covers:
  - trie.py:    TrieNode, Trie (insert, remove, search_prefix, autocomplete)
  - fuzzy.py:   levenshtein_distance, jaro_winkler_similarity, fuzzy_match, bulk_fuzzy_resolve
  - ranking.py: TFIDFIndex (add, remove, score, idf, lazy recomputation)
  - engine.py:  SearchEngine (add, add_many, remove, search, suggest, explain_query)

Run with:
  PYTHONPATH=src python -m pytest tests/test_search_engine.py -v
"""
import asyncio
import sys
import os

# Make search-service modules importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "search-service"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

# ─────────────────────────────────────────────────────────────────────────────
# Trie Tests
# ─────────────────────────────────────────────────────────────────────────────

from trie import Trie, TrieNode


class TestTrieNode:
    def test_default_state(self):
        node = TrieNode()
        assert node.children == {}
        assert node.is_end is False
        assert node.doc_ids == set()


class TestTrie:
    def test_insert_single_word(self):
        t = Trie()
        t.insert("payment", "doc1")
        assert "doc1" in t.search_prefix("pay")
        assert "doc1" in t.search_prefix("payment")

    def test_insert_multiple_docs_same_word(self):
        t = Trie()
        t.insert("payment", "doc1")
        t.insert("payment", "doc2")
        result = t.search_prefix("pay")
        assert "doc1" in result
        assert "doc2" in result

    def test_prefix_not_matching(self):
        t = Trie()
        t.insert("payment", "doc1")
        assert t.search_prefix("invoice") == set()

    def test_prefix_empty_returns_empty_on_empty_trie(self):
        t = Trie()
        assert t.search_prefix("") == set()

    def test_insert_and_prefix_search_exact(self):
        t = Trie()
        t.insert("billing", "doc3")
        assert "doc3" in t.search_prefix("billing")

    def test_remove_doc_from_word(self):
        t = Trie()
        t.insert("payment", "doc1")
        t.insert("payment", "doc2")
        t.remove("payment", "doc1")
        result = t.search_prefix("pay")
        assert "doc1" not in result
        assert "doc2" in result

    def test_remove_nonexistent_word_no_crash(self):
        t = Trie()
        t.remove("nonexistent", "doc1")  # should not raise

    def test_autocomplete_single(self):
        t = Trie()
        t.insert("payment", "doc1")
        t.insert("payroll", "doc2")
        t.insert("invoice", "doc3")
        suggestions = t.autocomplete("pay")
        assert "payment" in suggestions
        assert "payroll" in suggestions
        assert "invoice" not in suggestions

    def test_autocomplete_lexicographic_order(self):
        t = Trie()
        t.insert("payroll", "doc1")
        t.insert("payment", "doc2")
        t.insert("paycheck", "doc3")
        suggestions = t.autocomplete("pay")
        assert suggestions == sorted(suggestions)

    def test_autocomplete_max_results(self):
        t = Trie()
        for i in range(20):
            t.insert(f"word{i:02d}", f"doc{i}")
        suggestions = t.autocomplete("word", max_suggestions=5)
        assert len(suggestions) <= 5

    def test_autocomplete_no_prefix_match(self):
        t = Trie()
        t.insert("payment", "doc1")
        assert t.autocomplete("xyz") == []

    def test_all_words(self):
        t = Trie()
        words = ["alpha", "beta", "gamma"]
        for w in words:
            t.insert(w, "doc1")
        all_w = t.all_words()
        assert set(all_w) == set(words)


# ─────────────────────────────────────────────────────────────────────────────
# Fuzzy Tests
# ─────────────────────────────────────────────────────────────────────────────

from fuzzy import levenshtein_distance, jaro_winkler_similarity, fuzzy_match, bulk_fuzzy_resolve


class TestLevenshtein:
    def test_identical_strings(self):
        assert levenshtein_distance("payment", "payment") == 0

    def test_empty_and_nonempty(self):
        assert levenshtein_distance("", "abc") == 3
        assert levenshtein_distance("abc", "") == 3

    def test_both_empty(self):
        assert levenshtein_distance("", "") == 0

    def test_single_insertion(self):
        # "paymnt" needs 1 insertion to become "payment"
        assert levenshtein_distance("paymnt", "payment") == 1

    def test_transposition_counts_as_two(self):
        # Levenshtein treats transposition as delete+insert = 2
        assert levenshtein_distance("ab", "ba") == 2

    def test_completely_different(self):
        assert levenshtein_distance("abc", "xyz") == 3

    def test_known_pair_kitten_sitting(self):
        assert levenshtein_distance("kitten", "sitting") == 3

    def test_known_pair_saturday_sunday(self):
        assert levenshtein_distance("saturday", "sunday") == 3


class TestJaroWinkler:
    def test_identical(self):
        assert jaro_winkler_similarity("payment", "payment") == pytest.approx(1.0)

    def test_empty_strings(self):
        assert jaro_winkler_similarity("", "") == pytest.approx(1.0)

    def test_one_empty(self):
        assert jaro_winkler_similarity("payment", "") == pytest.approx(0.0)

    def test_typo_score_high(self):
        score = jaro_winkler_similarity("payment", "paymnet")
        assert score > 0.9

    def test_different_strings_lower_score(self):
        score = jaro_winkler_similarity("payment", "invoice")
        assert score < 0.6


class TestFuzzyMatch:
    def test_exact_match_distance_zero(self):
        results = fuzzy_match("payment", ["payment", "invoice", "billing"], max_distance=2)
        assert any(w == "payment" and d == 0 for w, d in results)

    def test_typo_within_distance(self):
        results = fuzzy_match("paymnt", ["payment", "invoice"], max_distance=2)
        assert any(w == "payment" for w, d in results)

    def test_beyond_max_distance_excluded(self):
        results = fuzzy_match("abc", ["xyz"], max_distance=1)
        assert results == []

    def test_results_sorted_by_distance(self):
        results = fuzzy_match("pay", ["payment", "pay", "payroll"], max_distance=5)
        distances = [d for _, d in results]
        assert distances == sorted(distances)

    def test_length_filter_optimization(self):
        # "a" vs "abcdefgh" => diff = 7 > max_distance = 2, must be excluded
        results = fuzzy_match("a", ["abcdefgh"], max_distance=2)
        assert results == []


class TestBulkFuzzyResolve:
    def test_basic(self):
        inverted = {
            "payment": {"doc1", "doc2"},
            "invoice": {"doc3"},
        }
        result = bulk_fuzzy_resolve(["paymnt"], inverted, max_distance=2)
        assert "doc1" in result
        assert "doc2" in result

    def test_best_distance_per_doc(self):
        inverted = {
            "pay": {"doc1"},
            "payment": {"doc1"},
        }
        # "pay" exact match dist=0, "payment" dist=4 from "pay" — best should be 0
        result = bulk_fuzzy_resolve(["pay"], inverted, max_distance=4)
        assert result.get("doc1") == 0

    def test_empty_query_tokens(self):
        inverted = {"payment": {"doc1"}}
        result = bulk_fuzzy_resolve([], inverted, max_distance=2)
        assert result == {}

    def test_empty_index(self):
        result = bulk_fuzzy_resolve(["payment"], {}, max_distance=2)
        assert result == {}


# ─────────────────────────────────────────────────────────────────────────────
# TF-IDF Ranking Tests
# ─────────────────────────────────────────────────────────────────────────────

from ranking import TFIDFIndex, _tokenize


class TestTokenize:
    def test_lowercase_split(self):
        assert _tokenize("Payment Service") == ["payment", "service"]

    def test_strips_punctuation(self):
        assert _tokenize("Hello, World!") == ["hello", "world"]

    def test_numbers_kept(self):
        assert "3" in _tokenize("Version 3")

    def test_empty_string(self):
        assert _tokenize("") == []


class TestTFIDFIndex:
    def test_add_and_score(self):
        idx = TFIDFIndex()
        idx.add_document("doc1", "payment billing service")
        idx.add_document("doc2", "invoice management")
        score = idx.score("doc1", ["payment"])
        assert score > 0.0

    def test_document_not_in_index_returns_zero(self):
        idx = TFIDFIndex()
        idx.add_document("doc1", "payment")
        assert idx.score("doc999", ["payment"]) == 0.0

    def test_more_discriminative_term_higher_idf(self):
        idx = TFIDFIndex()
        idx.add_document("doc1", "payment billing service")
        idx.add_document("doc2", "payment invoice service")
        idx.add_document("doc3", "payment refund service")
        # "billing" appears in 1 doc; "payment" appears in 3 => billing IDF > payment IDF
        assert idx.idf("billing") > idx.idf("payment")

    def test_remove_document(self):
        idx = TFIDFIndex()
        idx.add_document("doc1", "payment billing")
        idx.remove_document("doc1")
        assert idx.score("doc1", ["payment"]) == 0.0

    def test_reindex_updates_score(self):
        idx = TFIDFIndex()
        idx.add_document("doc1", "payment service")
        idx.add_document("doc1", "invoice management system")  # re-index
        assert idx.score("doc1", ["payment"]) == 0.0
        assert idx.score("doc1", ["invoice"]) > 0.0

    def test_document_count_after_add_remove(self):
        idx = TFIDFIndex()
        idx.add_document("doc1", "test one")
        idx.add_document("doc2", "test two")
        assert idx.document_count == 2
        idx.remove_document("doc1")
        assert idx.document_count == 1

    def test_vocabulary_size(self):
        idx = TFIDFIndex()
        idx.add_document("doc1", "payment billing")
        assert idx.vocabulary_size == 2

    def test_top_terms_returns_list(self):
        idx = TFIDFIndex()
        idx.add_document("doc1", "payment billing service")
        terms = idx.top_terms(10)
        assert isinstance(terms, list)
        assert len(terms) <= 3  # only 3 unique tokens


# ─────────────────────────────────────────────────────────────────────────────
# SearchEngine Integration Tests (async)
# ─────────────────────────────────────────────────────────────────────────────

from engine import SearchEngine


def run(coro):
    """Helper to run async coroutines in sync test context."""
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture
def engine():
    return SearchEngine()


@pytest.fixture
def populated_engine():
    eng = SearchEngine()
    run(eng.add_many([
        {"id": "repo_001", "type": "Repository", "text": "payment service golang microservice billing", "metadata": {}},
        {"id": "repo_002", "type": "Repository", "text": "invoice management python backend", "metadata": {}},
        {"id": "repo_003", "type": "Repository", "text": "payment gateway java spring billing", "metadata": {}},
        {"id": "doc_001",  "type": "Document",   "text": "README payment service documentation", "metadata": {}},
    ]))
    return eng


class TestSearchEngineAdd:
    def test_add_single(self, engine):
        run(engine.add("doc1", "Repository", "payment service", {}))
        assert engine.stats()["totalDocuments"] == 1

    def test_add_many(self, engine):
        run(engine.add_many([
            {"id": "a", "type": "T", "text": "alpha", "metadata": {}},
            {"id": "b", "type": "T", "text": "beta",  "metadata": {}},
        ]))
        assert engine.stats()["totalDocuments"] == 2

    def test_add_idempotent_update(self, engine):
        run(engine.add("doc1", "T", "payment", {}))
        run(engine.add("doc1", "T", "invoice", {}))
        assert engine.stats()["totalDocuments"] == 1
        results = run(engine.search("invoice"))
        assert any(r["id"] == "doc1" for r in results)

    def test_remove_existing(self, engine):
        run(engine.add("doc1", "T", "payment", {}))
        removed = run(engine.remove("doc1"))
        assert removed is True
        assert engine.stats()["totalDocuments"] == 0

    def test_remove_nonexistent(self, engine):
        removed = run(engine.remove("nonexistent"))
        assert removed is False


class TestSearchEngineSearch:
    def test_exact_match(self, populated_engine):
        results = run(populated_engine.search("billing"))
        ids = [r["id"] for r in results]
        assert "repo_001" in ids or "repo_003" in ids

    def test_prefix_match(self, populated_engine):
        results = run(populated_engine.search("pay", fuzzy=False))
        assert len(results) > 0

    def test_fuzzy_match_typo(self, populated_engine):
        results = run(populated_engine.search("paymnt", max_fuzzy_distance=2))
        ids = [r["id"] for r in results]
        assert "repo_001" in ids or "repo_003" in ids

    def test_entity_type_filter(self, populated_engine):
        results = run(populated_engine.search("payment", entity_type="Document"))
        assert all(r["type"] == "Document" for r in results)

    def test_empty_query_returns_empty(self, populated_engine):
        results = run(populated_engine.search("   "))
        assert results == []

    def test_no_results_for_nonexistent_term(self, populated_engine):
        results = run(populated_engine.search("xyzzy12345", fuzzy=False))
        assert results == []

    def test_match_type_exact(self, populated_engine):
        results = run(populated_engine.search("billing", fuzzy=False))
        exact = [r for r in results if r["matchType"] == "exact"]
        assert len(exact) > 0

    def test_match_type_fuzzy(self, populated_engine):
        results = run(populated_engine.search("paymnt", max_fuzzy_distance=2))
        fuzzy_results = [r for r in results if "fuzzy" in r["matchType"]]
        assert len(fuzzy_results) > 0

    def test_results_sorted_descending(self, populated_engine):
        results = run(populated_engine.search("payment billing"))
        scores = [r["score"] for r in results]
        assert scores == sorted(scores, reverse=True)

    def test_top_k_respected(self, populated_engine):
        results = run(populated_engine.search("payment", top_k=2))
        assert len(results) <= 2


class TestSearchEngineSuggest:
    def test_suggest_returns_completions(self, populated_engine):
        suggestions = populated_engine.suggest("pay")
        assert "payment" in suggestions

    def test_suggest_empty_prefix_no_crash(self, populated_engine):
        suggestions = populated_engine.suggest("")
        assert isinstance(suggestions, list)

    def test_suggest_no_match(self, populated_engine):
        suggestions = populated_engine.suggest("zzz")
        assert suggestions == []


class TestSearchEngineExplain:
    def test_explain_returns_tokens(self, populated_engine):
        result = populated_engine.explain_query("payment billing")
        assert "tokens" in result
        assert len(result["tokens"]) == 2

    def test_explain_token_fields(self, populated_engine):
        result = populated_engine.explain_query("payment")
        token = result["tokens"][0]
        assert "token" in token
        assert "idf" in token
        assert "documentFrequency" in token
        assert "prefixMatches" in token

    def test_explain_total_documents(self, populated_engine):
        result = populated_engine.explain_query("payment")
        assert result["totalDocuments"] == 4


class TestSearchEngineStats:
    def test_stats_empty_engine(self, engine):
        stats = engine.stats()
        assert stats["totalDocuments"] == 0
        assert stats["vocabularySize"] == 0

    def test_stats_after_add(self, engine):
        run(engine.add("doc1", "T", "payment billing service", {}))
        stats = engine.stats()
        assert stats["totalDocuments"] == 1
        assert stats["vocabularySize"] == 3

    def test_stats_indexed_count(self, engine):
        run(engine.add("d1", "T", "a", {}))
        run(engine.add("d2", "T", "b", {}))
        assert engine.stats()["totalIndexed"] == 2


# ─────────────────────────────────────────────────────────────────────────────
# Repository Service Logic Tests (no FastAPI / no HTTP)
# ─────────────────────────────────────────────────────────────────────────────

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "repository-service"))

from service import (
    CreateRepositoryRequest,
    UpdateRepositoryRequest,
    create_repository,
    get_repository,
    list_repositories,
    update_repository,
    delete_repository,
    sync_repository,
    _store,
    _url_index,
)


@pytest.fixture(autouse=True)
def clear_repo_store():
    """Isolate each test: clear in-memory store before and after."""
    _store.clear()
    _url_index.clear()
    yield
    _store.clear()
    _url_index.clear()


def make_req(**kwargs):
    defaults = dict(
        organizationId="org_001",
        name="payment-service",
        url="https://github.com/acme/payment-service",
        language="Go",
        createdBy="user_42",
    )
    defaults.update(kwargs)
    return CreateRepositoryRequest(**defaults)


class TestRepositoryService:
    def setup_method(self):
        """Reset in-memory store between tests to avoid state leakage."""
        import service as svc
        svc._store     = {}
        svc._url_index = {}
        svc._pool      = None

    def test_create_repository(self):
        record = run(create_repository(make_req()))
        assert record["name"] == "payment-service"
        assert record["organizationId"] == "org_001"
        assert record["status"] == "active"
        assert record["repositoryId"].startswith("repo_")

    def test_create_duplicate_url_raises(self):
        run(create_repository(make_req()))
        with pytest.raises(ValueError, match="already exists"):
            run(create_repository(make_req()))

    def test_create_invalid_url_raises(self):
        with pytest.raises(ValueError, match="Invalid repository URL"):
            run(create_repository(make_req(url="not-a-valid-url")))

    def test_get_found(self):
        record = run(create_repository(make_req()))
        fetched = run(get_repository(record["repositoryId"]))
        assert fetched is not None
        assert fetched["repositoryId"] == record["repositoryId"]

    def test_get_not_found(self):
        assert run(get_repository("nonexistent")) is None

    def test_list_all(self):
        run(create_repository(make_req(url="https://github.com/acme/s1", name="s1")))
        run(create_repository(make_req(url="https://github.com/acme/s2", name="s2")))
        repos = run(list_repositories())
        assert len(repos) == 2

    def test_list_filtered_by_org(self):
        run(create_repository(make_req(url="https://github.com/acme/s1", organizationId="org_001")))
        run(create_repository(make_req(url="https://github.com/acme/s2", organizationId="org_002")))
        repos = run(list_repositories("org_001"))
        assert len(repos) == 1
        assert repos[0]["organizationId"] == "org_001"

    def test_update_name(self):
        record = run(create_repository(make_req()))
        repo_id = record["repositoryId"]
        result = run(update_repository(repo_id, UpdateRepositoryRequest(name="new-name", updatedBy="u")))
        assert result is not None
        assert result["record"]["name"] == "new-name"
        assert "name" in result["changes"]

    def test_update_not_found(self):
        result = run(update_repository("nonexistent", UpdateRepositoryRequest(name="x", updatedBy="u")))
        assert result is None

    def test_update_no_actual_changes(self):
        record = run(create_repository(make_req()))
        result = run(update_repository(
            record["repositoryId"],
            UpdateRepositoryRequest(name="payment-service", updatedBy="u")
        ))
        assert result["changes"] == []

    def test_delete_repository(self):
        record = run(create_repository(make_req()))
        repo_id = record["repositoryId"]
        deleted = run(delete_repository(repo_id, "user_42"))
        assert deleted is not None
        assert run(get_repository(repo_id)) is None

    def test_delete_frees_url(self):
        req = make_req()
        record = run(create_repository(req))
        run(delete_repository(record["repositoryId"], "u"))
        # URL should be reusable now
        record2 = run(create_repository(req))
        assert record2["repositoryId"] != record["repositoryId"]

    def test_delete_nonexistent(self):
        assert run(delete_repository("nonexistent", "u")) is None

    def test_sync_updates_last_synced_at(self):
        record = run(create_repository(make_req()))
        assert record["lastSyncedAt"] is None
        synced = run(sync_repository(record["repositoryId"]))
        assert synced["lastSyncedAt"] is not None

    def test_sync_nonexistent(self):
        assert run(sync_repository("nonexistent")) is None


# ─────────────────────────────────────────────────────────────────────────────
# Shared Models Tests
# ─────────────────────────────────────────────────────────────────────────────

from shared.models import (
    EventEnvelope,
    RepositoryCreatedPayload,
    DocumentProcessedPayload,
    create_event,
)


class TestSharedModels:
    def test_create_event_sets_all_fields(self):
        payload = RepositoryCreatedPayload(
            repositoryId="repo_001",
            organizationId="org_001",
            name="svc",
            url="https://github.com/acme/svc",
            language="Go",
            createdBy="user_1",
        )
        env = create_event("RepositoryCreated", "repo_001", "org_001", payload)
        assert env.event_type == "RepositoryCreated"
        assert env.aggregate_id == "repo_001"
        assert env.organization_id == "org_001"
        assert env.event_version == 1
        assert env.event_id is not None
        assert env.correlation_id is not None

    def test_event_serialization(self):
        payload = DocumentProcessedPayload(
            documentId="doc_001",
            repositoryId="repo_001",
            documentType="MARKDOWN",
            fileName="README.md",
            chunkCount=3,
            wordCount=150,
        )
        env = create_event("DocumentProcessed", "doc_001", "org_001", payload)
        data = env.model_dump(by_alias=True)
        assert data["eventType"] == "DocumentProcessed"
        assert data["payload"]["documentId"] == "doc_001"

    def test_repository_created_payload_defaults(self):
        payload = RepositoryCreatedPayload(
            repositoryId="r1",
            organizationId="o1",
            name="svc",
            url="https://github.com/x/y",
            language="Python",
            createdBy="u1",
        )
        assert payload.visibility == "private"
        assert payload.default_branch == "main"

    def test_unique_event_ids(self):
        payload = RepositoryCreatedPayload(
            repositoryId="r1", organizationId="o1", name="s",
            url="https://github.com/x/y", language="Go", createdBy="u"
        )
        e1 = create_event("RepositoryCreated", "r1", "o1", payload)
        e2 = create_event("RepositoryCreated", "r1", "o1", payload)
        assert e1.event_id != e2.event_id
